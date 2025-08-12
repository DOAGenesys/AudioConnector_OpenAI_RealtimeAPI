import asyncio
import uuid
import json
import time
import websockets

from config import (
    logger,
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_PHASES,
    GENESYS_MSG_RATE_LIMIT,
    GENESYS_BINARY_RATE_LIMIT,
    GENESYS_MSG_BURST_LIMIT,
    GENESYS_BINARY_BURST_LIMIT,
    DEFAULT_AGENT_NAME,
    DEFAULT_COMPANY_NAME,
    DEFAULT_MAX_OUTPUT_TOKENS,
    AUDIO_FRAME_SEND_INTERVAL,
    MAX_AUDIO_BUFFER_SIZE,
    ENDING_PROMPT,
    ENDING_TEMPERATURE
)

from rate_limiter import RateLimiter
from openai_client import OpenAIRealtimeClient
from utils import format_json, parse_iso8601_duration

from collections import deque

class AudioHookServer:
    def __init__(self, websocket):
        self.session_id = str(uuid.uuid4())
        self.ws = websocket
        self.client_seq = 0
        self.server_seq = 0
        self.openai_client = None
        self.running = True
        self.negotiated_media = None
        self.start_time = time.time()
        self.logger = logger.getChild(f"AudioHookServer_{self.session_id}")
        self.audio_frames_sent = 0
        self.audio_frames_received = 0
        self.rate_limit_state = {
            "retry_count": 0,
            "last_retry_time": 0,
            "in_backoff": False
        }

        self.message_limiter = RateLimiter(GENESYS_MSG_RATE_LIMIT, GENESYS_MSG_BURST_LIMIT)
        self.binary_limiter = RateLimiter(GENESYS_BINARY_RATE_LIMIT, GENESYS_BINARY_BURST_LIMIT)

        self.audio_buffer = deque(maxlen=MAX_AUDIO_BUFFER_SIZE)
        self.audio_process_task = None
        self.last_frame_time = 0

        self.logger.info(f"New session started: {self.session_id}")        

    async def start_audio_processing(self):
        self.audio_process_task = asyncio.create_task(self._process_audio_buffer())

    async def stop_audio_processing(self):
        if self.audio_process_task:
            self.audio_process_task.cancel()
            try:
                await self.audio_process_task
            except asyncio.CancelledError:
                pass
            self.audio_process_task = None

    async def _process_audio_buffer(self):
        try:
            while self.running:
                if self.audio_buffer:
                    now = time.time()
                    if now - self.last_frame_time >= AUDIO_FRAME_SEND_INTERVAL:
                        if await self.binary_limiter.acquire():
                            frame = self.audio_buffer.popleft()
                            try:
                                await self.ws.send(frame)
                                self.audio_frames_sent += 1
                                self.last_frame_time = now
                                self.logger.debug(
                                    f"Sent audio frame from buffer: {len(frame)} bytes "
                                    f"(frame #{self.audio_frames_sent}, buffer size: {len(self.audio_buffer)})"
                                )
                            except websockets.ConnectionClosed:
                                self.logger.warning("Genesys WebSocket closed while sending audio frame.")
                                self.running = False
                                break
                        else:
                            await asyncio.sleep(AUDIO_FRAME_SEND_INTERVAL)
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.logger.info("Audio processing task cancelled")
        except Exception as e:
            self.logger.error(f"Error in audio processing task: {e}", exc_info=True)

    async def handle_error(self, msg: dict):
        error_code = msg["parameters"].get("code")
        error_params = msg["parameters"]

        if error_code == 429:
            retry_after = None

            if "retryAfter" in error_params:
                retry_after_duration = error_params["retryAfter"]
                try:
                    retry_after = parse_iso8601_duration(retry_after_duration)
                    self.logger.info(
                        f"[Rate Limit] Using Genesys-provided retryAfter duration: {retry_after}s "
                        f"(parsed from {retry_after_duration})"
                    )
                except ValueError as e:
                    self.logger.warning(
                        f"[Rate Limit] Failed to parse Genesys retryAfter format: {retry_after_duration}. "
                        f"Error: {str(e)}"
                    )

            if retry_after is None and hasattr(self.ws, 'response_headers'):
                http_retry_after = (
                    self.ws.response_headers.get('Retry-After') or 
                    self.ws.response_headers.get('retry-after')
                )
                if http_retry_after:
                    try:
                        retry_after = float(http_retry_after)
                        self.logger.info(
                            f"[Rate Limit] Using HTTP header Retry-After duration: {retry_after}s"
                        )
                    except ValueError:
                        try:
                            retry_after = parse_iso8601_duration(http_retry_after)
                            self.logger.info(
                                f"[Rate Limit] Using HTTP header Retry-After duration: {retry_after}s "
                                f"(parsed from ISO8601)"
                            )
                        except ValueError:
                            self.logger.warning(
                                f"[Rate Limit] Failed to parse HTTP Retry-After format: {http_retry_after}"
                            )

            self.logger.warning(
                f"[Rate Limit] Received 429 error. "
                f"Session: {self.session_id}, "
                f"Current duration: {time.time() - self.start_time:.2f}s, "
                f"Retry count: {self.rate_limit_state['retry_count']}, "
                f"RetryAfter: {retry_after}s"
            )

            self.rate_limit_state["in_backoff"] = True
            self.rate_limit_state["retry_count"] += 1

            if self.rate_limit_state["retry_count"] > RATE_LIMIT_MAX_RETRIES:
                self.logger.error(
                    f"[Rate Limit] Max retries ({RATE_LIMIT_MAX_RETRIES}) exceeded. "
                    f"Session: {self.session_id}, "
                    f"Total retries: {self.rate_limit_state['retry_count']}, "
                    f"Duration: {time.time() - self.start_time:.2f}s"
                )
                await self.disconnect_session(reason="error", info="Rate limit max retries exceeded")
                return False

            if self.openai_client:
                self.openai_client.running = False
            self.running = False

            session_duration = time.time() - self.start_time

            if retry_after is not None:
                used_delay = retry_after
                self.logger.info(
                    f"[Rate Limit] Using provided retry delay: {used_delay}s. "
                    f"Session: {self.session_id}"
                )
            else:
                delay = None
                for phase in RATE_LIMIT_PHASES:
                    if session_duration <= phase["window"]:
                        delay = phase["delay"]
                        break
                if delay is None:
                    delay = RATE_LIMIT_PHASES[-1]["delay"]
                used_delay = delay
                self.logger.info(
                    f"[Rate Limit] Using default exponential backoff delay: {used_delay}s. "
                    f"Session: {self.session_id}"
                )

            self.logger.warning(
                f"[Rate Limit] Rate limited, attempt {self.rate_limit_state['retry_count']}/{RATE_LIMIT_MAX_RETRIES}. "
                f"Backing off for {used_delay}s. "
                f"Session: {self.session_id}, "
                f"Duration: {session_duration:.2f}s"
            )

            await asyncio.sleep(used_delay)

            self.running = True
            if self.openai_client:
                self.openai_client.running = True

            self.rate_limit_state["in_backoff"] = False
            self.logger.info(
                f"[Rate Limit] Backoff complete, resuming operations. "
                f"Session: {self.session_id}"
            )

            return True
        return False

    async def handle_message(self, msg: dict):
        msg_type = msg.get("type")
        seq = msg.get("seq", 0)
        self.client_seq = seq

        if self.rate_limit_state.get("in_backoff") and msg_type != "error":
            self.logger.debug(f"Skipping message type {msg_type} during rate limit backoff")
            return

        if msg_type == "error":
            handled = await self.handle_error(msg)
            if handled:
                return

        if msg_type == "open":
            await self.handle_open(msg)
        elif msg_type == "ping":
            await self.handle_ping(msg)
        elif msg_type == "close":
            await self.handle_close(msg)
        elif msg_type in ["update", "resume", "pause"]:
            self.logger.debug(f"Ignoring message of type {msg_type}")
        else:
            self.logger.debug(f"Ignoring unknown message type: {msg_type}")

    async def handle_open(self, msg: dict):
        self.session_id = msg["id"]

        is_probe = (
            msg["parameters"].get("conversationId") == "00000000-0000-0000-0000-000000000000" and
            msg["parameters"].get("participant", {}).get("id") == "00000000-0000-0000-0000-000000000000"
        )

        if is_probe:
            self.logger.info("Detected probe connection")
            opened_msg = {
                "version": "2",
                "type": "opened",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "startPaused": False,
                    "media": []
                }
            }
            self.server_seq += 1
            await self._send_json(opened_msg)
            return

        offered_media = msg["parameters"].get("media", [])
        chosen = None
        for m in offered_media:
            if (m.get("format") == "PCMU" and m.get("rate") == 8000):
                chosen = m
                break

        if not chosen:
            resp = {
                "version": "2",
                "type": "disconnect",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "reason": "error",
                    "info": "No supported format found"
                }
            }
            self.server_seq += 1
            await self._send_json(resp)
            self.running = False
            return

        opened_msg = {
            "version": "2",
            "type": "opened",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {
                "startPaused": False,
                "media": [chosen]
            }
        }
        self.server_seq += 1
        await self._send_json(opened_msg)
        self.logger.info(f"Session opened. Negotiated media format: {chosen}")

        input_vars = msg["parameters"].get("inputVariables", {})
        voice = input_vars.get("OPENAI_VOICE", "sage")
        instructions = input_vars.get("OPENAI_SYSTEM_PROMPT", "You are a helpful assistant.")
        temperature = input_vars.get("OPENAI_TEMPERATURE")
        model = input_vars.get("OPENAI_MODEL")
        max_output_tokens = input_vars.get("OPENAI_MAX_OUTPUT_TOKENS")
        language = input_vars.get("LANGUAGE")
        customer_data = input_vars.get("CUSTOMER_DATA")
        agent_name = input_vars.get("AGENT_NAME", DEFAULT_AGENT_NAME)
        company_name = next((value for key, value in input_vars.items() 
                            if key.strip() == "COMPANY_NAME"), DEFAULT_COMPANY_NAME)

        self.logger.info(f"Using voice: {voice}")
        self.logger.debug(f"Using instructions: {instructions}")
        if temperature:
            self.logger.debug(f"Using temperature: {temperature}")
        if model:
            self.logger.debug(f"Using model: {model}")
        if max_output_tokens:
            if str(max_output_tokens).lower() == 'inf':
                max_output_tokens = "inf"
            else:
                try:
                    tokens = int(max_output_tokens)
                    if 1 <= tokens <= 4096:
                        max_output_tokens = tokens
                    else:
                        self.logger.warning(f"max_output_tokens {tokens} out of range [1, 4096]. Using default: {DEFAULT_MAX_OUTPUT_TOKENS}")
                        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
                except (TypeError, ValueError):
                    self.logger.warning(f"Invalid max_output_tokens value: {max_output_tokens}. Using default: {DEFAULT_MAX_OUTPUT_TOKENS}")
                    max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
        else:
            max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
        if language:
            self.logger.info(f"Enforcing language: {language}")
        if customer_data:
            self.logger.debug("Customer data provided for personalization")

        try:
            self.openai_client = OpenAIRealtimeClient(self.session_id, on_speech_started_callback=self.handle_speech_started)
            self.openai_client.language = language
            self.openai_client.customer_data = customer_data
            await self.openai_client.connect(
                instructions=instructions,
                voice=voice,
                temperature=temperature,
                model=model,
                max_output_tokens=max_output_tokens,
                agent_name=agent_name,
                company_name=company_name
            )
        except Exception as e:
            self.logger.error(f"OpenAI connection failed: {e}")
            await self.disconnect_session(reason="error", info=str(e))
            return

        def on_audio_callback(pcmu_8k: bytes):
            asyncio.create_task(self.handle_openai_audio(pcmu_8k))

        await self.start_audio_processing()
        await self.openai_client.start_receiving(on_audio_callback)

    async def handle_speech_started(self):
        event_msg = {
            "version": "2",
            "type": "event",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {
                "entities": [
                    {
                        "type": "barge_in",
                        "data": {}
                    }
                ]
            }
        }
        self.server_seq += 1
        await self._send_json(event_msg)

    async def handle_openai_audio(self, pcmu_8k: bytes):
        if not self.running:
            return
        self.logger.debug(f"Processing OpenAI audio frame: {len(pcmu_8k)} bytes")

        await self.send_binary_to_genesys(pcmu_8k)

    async def send_binary_to_genesys(self, data: bytes):
        if len(self.audio_buffer) < MAX_AUDIO_BUFFER_SIZE:
            self.audio_buffer.append(data)
            self.logger.debug(
                f"Buffered audio frame: {len(data)} bytes "
                f"(buffer size: {len(self.audio_buffer)})"
            )
        else:
            self.logger.warning(
                f"Audio buffer full ({len(self.audio_buffer)} frames), "
                f"dropping frame to prevent overflow"
            )

    async def handle_ping(self, msg: dict):
        pong_msg = {
            "version": "2",
            "type": "pong",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {}
        }
        self.server_seq += 1
        try:
            await asyncio.wait_for(self._send_json(pong_msg), timeout=1.0)
        except asyncio.TimeoutError:
            self.logger.error("Failed to send pong response within timeout")

    async def generate_session_summary(self):
        if not self.openai_client:
            return None

        try:
            ending_prompt = {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "modalities": ["text"],
                    "metadata": {"type": "ending_analysis"},
                    "instructions": ENDING_PROMPT,
                    "temperature": ENDING_TEMPERATURE
                }
            }

            await self.openai_client._safe_send(json.dumps(ending_prompt))
            
            summary = None
            try:
                async with asyncio.timeout(10): 
                    while True:
                        msg = await self.openai_client.ws.recv()
                        data = json.loads(msg)
                        if (data.get("type") == "response.done" and 
                            data.get("response", {}).get("metadata", {}).get("type") == "ending_analysis"):
                            summary = data.get("response", {}).get("output", [{}])[0].get("text")
                            break
                
                # Parse JSON summary
                if summary:
                    try:
                        summary_dict = json.loads(summary)
                        return summary_dict
                    except json.JSONDecodeError:
                        self.logger.error("Failed to parse summary JSON")
                        return {"error": "Failed to parse summary"}
                    
            except asyncio.TimeoutError:
                self.logger.error("Timeout generating session summary")
                return {"error": "Timeout generating summary"}
                
        except Exception as e:
            self.logger.error(f"Error generating session summary: {e}")
            return {"error": str(e)}

    async def handle_close(self, msg: dict):
        """
        Update the handle_close method to include summary generation
        """
        self.logger.info(f"Received 'close' from Genesys. Reason: {msg['parameters'].get('reason')}")
        
        # Generate summary before closing
        summary = await self.generate_session_summary()
        if summary:
            self.logger.info(f"Session summary: {summary}")

        closed_msg = {
            "version": "2",
            "type": "closed",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {
                "summary": summary
            }
        }
        self.server_seq += 1
        try:
            await asyncio.wait_for(self._send_json(closed_msg), timeout=4.0)
        except asyncio.TimeoutError:
            self.logger.error("Failed to send closed response within timeout")

        duration = time.time() - self.start_time
        self.logger.info(
            f"Session stats - Duration: {duration:.2f}s, "
            f"Frames sent: {self.audio_frames_sent}, "
            f"Frames received: {self.audio_frames_received}"
        )

        await self.stop_audio_processing()
        if self.openai_client:
            await self.openai_client.close()
        self.running = False


    async def disconnect_session(self, reason="completed", info=""):
        try:
            if not self.session_id:
                return

            # Generate summary before disconnecting
            summary_data = await self.generate_session_summary()

            # Get token usage from OpenAI client's last response if available
            token_metrics = {"usage": {}}
            if self.openai_client and hasattr(self.openai_client, 'last_response'):
                usage = self.openai_client.last_response.get("usage", {})
                token_details = usage.get("input_token_details", {})
                cached_details = token_details.get("cached_tokens_details", {})
                output_details = usage.get("output_token_details", {})
                
                token_metrics = {
                    "TOTAL_INPUT_TEXT_TOKENS": str(token_details.get("text_tokens", 0)),
                    "TOTAL_INPUT_CACHED_TEXT_TOKENS": str(cached_details.get("text_tokens", 0)),
                    "TOTAL_INPUT_AUDIO_TOKENS": str(token_details.get("audio_tokens", 0)),
                    "TOTAL_INPUT_CACHED_AUDIO_TOKENS": str(cached_details.get("audio_tokens", 0)),
                    "TOTAL_OUTPUT_TEXT_TOKENS": str(output_details.get("text_tokens", 0)),
                    "TOTAL_OUTPUT_AUDIO_TOKENS": str(output_details.get("audio_tokens", 0))
                }

            disconnect_msg = {
                "version": "2", 
                "type": "disconnect",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "reason": reason,
                    "info": info,
                    "outputVariables": {
                        "CONVERSATION_SUMMARY": json.dumps(summary_data) if summary_data else "",
                        "CONVERSATION_DURATION": str(time.time() - self.start_time),
                        **token_metrics
                    }
                }
            }
            self.server_seq += 1
            await asyncio.wait_for(self._send_json(disconnect_msg), timeout=5.0)
            try:
                await asyncio.wait_for(self.ws.wait_closed(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"Client did not acknowledge disconnect for session {self.session_id}")
        except Exception as e:
            logger.error(f"Error in disconnect_session: {e}")
        finally:
            self.running = False
            await self.stop_audio_processing()
            if self.openai_client:
                await self.openai_client.close()

    async def handle_audio_frame(self, frame_bytes: bytes):
        if not self.openai_client or not self.openai_client.running:
            return

        self.audio_frames_received += 1
        self.logger.debug(f"Received audio frame from Genesys: {len(frame_bytes)} bytes (frame #{self.audio_frames_received})")

        await self.openai_client.send_audio(frame_bytes)

    async def _send_json(self, msg: dict):
        try:
            if not await self.message_limiter.acquire():
                current_rate = self.message_limiter.get_current_rate()
                self.logger.warning(
                    f"Message rate limit exceeded (current rate: {current_rate:.2f}/s). "
                    f"Message type: {msg.get('type')}. Dropping to maintain compliance."
                )
                return

            self.logger.debug(f"Sending message to Genesys:\n{format_json(msg)}")
            await self.ws.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            self.logger.warning("Genesys WebSocket closed while sending JSON message.")
            self.running = False
