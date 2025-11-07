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
    MAX_AUDIO_BUFFER_SIZE,
    AUDIO_BUFFER_WARNING_THRESHOLD_HIGH,
    AUDIO_BUFFER_WARNING_THRESHOLD_MEDIUM,
    ENDING_PROMPT,
    ENDING_TEMPERATURE
)

from rate_limiter import RateLimiter
from openai_client import OpenAIRealtimeClient
from utils import format_json, parse_iso8601_duration
from genesys_actions import build_genesys_tool_context
from mcp_tools import load_mcp_tool_context

from collections import deque

class AudioHookServer:
    def __init__(self, websocket):
        """
        Initializes the instance of a class responsible for managing a websocket connection for
        audio integration, alongside managing state, rate limits, audio buffer, and task scheduling.

        :param websocket: An object representing the websocket connection to manage.
        :type websocket: any
        """
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
        self.genesys_tool_context = None
        self.session_outcome = {
            "escalation_required": False,
            "escalation_reason": "",
            "completion_summary": ""
        }

        self.logger.info(f"New session started: {self.session_id}")

    async def start_audio_processing(self):
        """
        Starts the audio processing task asynchronously.

        This method creates an asyncio task to process the audio buffer. It is
        responsible for initializing and triggering the process that handles
        the audio data in an asynchronous manner.

        :return: None
        """
        self.audio_process_task = asyncio.create_task(self._process_audio_buffer())

    async def stop_audio_processing(self):
        """
        Stops the currently running audio processing task, if there is one.

        This method checks if an audio processing task is active. If active, it cancels
        the task and awaits its termination. The method handles the case where the task
        may have been canceled.

        :return: None
        """
        if self.audio_process_task:
            self.audio_process_task.cancel()
            try:
                await self.audio_process_task
            except asyncio.CancelledError:
                pass
            self.audio_process_task = None

    async def _process_audio_buffer(self):
        """
        Process and send audio frames from the buffer asynchronously. Continuously monitors and sends
        audio frames from a buffer to a WebSocket connection as fast as the rate limiter allows.
        Genesys handles audio playback timing based on the PCMU format - we just need to send frames
        without artificial delays.

        :raises asyncio.CancelledError: If the task is canceled during execution.
        :raises Exception: If an unexpected error occurs during audio processing.
        """
        try:
            while self.running:
                if self.audio_buffer:
                    if await self.binary_limiter.acquire():
                        frame_bytes = self.audio_buffer.popleft()
                        try:
                            await self.ws.send(frame_bytes)
                            self.audio_frames_sent += 1
                            self.logger.debug(
                                f"Sent audio frame from buffer: {len(frame_bytes)} bytes "
                                f"(frame #{self.audio_frames_sent}, buffer size: {len(self.audio_buffer)})"
                            )
                        except websockets.ConnectionClosed:
                            self.logger.warning("Genesys WebSocket closed while sending audio frame.")
                            self.running = False
                            break
                    else:
                        await asyncio.sleep(0.01)
                else:
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
        """
        Handle the incoming message from Genesys audio hook server
        :param msg:
        :return:
        """
        msg_type = msg.get("type")
        seq = msg.get("seq", 0)
        self.client_seq = seq

        if self.rate_limit_state.get("in_backoff") and msg_type != "error":
            self.logger.debug(f"Skipping message type {msg_type} during rate limit backoff")
            return

        if msg_type == "error":
            handled = await self.handle_error(msg)
            # TODO: If not handled then falls through checking for other types that it cannot be
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

        try:
            self.genesys_tool_context = await build_genesys_tool_context(self.logger, input_vars)
        except Exception as exc:
            self.logger.error(f"[GenesysTools] Failed to prepare data action tools: {exc}")
            self.genesys_tool_context = None
        tool_definitions = []
        tool_instruction_blocks = []
        if self.genesys_tool_context:
            tool_definitions.extend(self.genesys_tool_context.tools)
            if self.genesys_tool_context.instructions:
                tool_instruction_blocks.append(self.genesys_tool_context.instructions)
            self.logger.info(f"[FunctionCall] Enabled {len(self.genesys_tool_context.tools)} Genesys data action tools for this session")

        mcp_raw_json = input_vars.get("MCP_TOOLS_JSON")
        mcp_context = load_mcp_tool_context(mcp_raw_json, self.logger)
        if mcp_context:
            tool_definitions.extend(mcp_context.tools)
            if mcp_context.instructions:
                tool_instruction_blocks.append(mcp_context.instructions)
            self.logger.info(f"[MCP] Enabled {len(mcp_context.tools)} MCP/built-in tool definitions for this session")

        tool_definitions_payload = tool_definitions if tool_definitions else None
        tool_instructions = "\n\n".join(block for block in tool_instruction_blocks if block) if tool_instruction_blocks else None

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
            # Wire callbacks for function-calling driven disconnects
            self.openai_client.on_end_call_request = self._on_end_call_request
            self.openai_client.on_handoff_request = self._on_handoff_request
            self.logger.info("[FunctionCall] OpenAI callbacks wired: on_end_call_request and on_handoff_request")
            if self.genesys_tool_context:
                self.openai_client.register_genesys_tool_handlers(self.genesys_tool_context.handlers)
            await self.openai_client.connect(
                instructions=instructions,
                voice=voice,
                temperature=temperature,
                model=model,
                max_output_tokens=max_output_tokens,
                agent_name=agent_name,
                company_name=company_name,
                tool_definitions=tool_definitions_payload,
                tool_instructions=tool_instructions
            )
        except Exception as e:
            self.logger.error(f"OpenAI connection failed: {e}")
            await self.disconnect_session(reason="error", info=str(e))
            return

        def on_audio_callback(pcmu_8k: bytes):
            asyncio.create_task(self.handle_openai_audio(pcmu_8k))

        await self.start_audio_processing()
        await self.openai_client.start_receiving(on_audio_callback)

    async def _on_end_call_request(self, reason: str, info: str):
        self.logger.info(f"[FunctionCall] OpenAI requested end_call. reason={reason}, info={info}")
        summary_text = info or reason or "Call completed"
        self.session_outcome.update({
            "escalation_required": False,
            "escalation_reason": "",
            "completion_summary": summary_text
        })
        await self.disconnect_session(reason=reason or "completed", info=info or "")

    async def _on_handoff_request(self, reason: str, info: str):
        self.logger.info(f"[FunctionCall] OpenAI requested handoff_to_human. reason={reason}, info={info}")
        escalation_reason = info or reason or "handoff_to_human"
        self.session_outcome.update({
            "escalation_required": True,
            "escalation_reason": escalation_reason,
            "completion_summary": ""
        })
        await self.disconnect_session(reason=reason or "transfer", info=info or "handoff_to_human")

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
            
            buffer_usage = len(self.audio_buffer) / MAX_AUDIO_BUFFER_SIZE
            
            if buffer_usage >= AUDIO_BUFFER_WARNING_THRESHOLD_HIGH:
                self.logger.warning(
                    f"Audio buffer usage HIGH: {len(self.audio_buffer)}/{MAX_AUDIO_BUFFER_SIZE} frames "
                    f"({buffer_usage*100:.1f}%) - Long response in progress"
                )
            elif buffer_usage >= AUDIO_BUFFER_WARNING_THRESHOLD_MEDIUM:
                self.logger.info(
                    f"Audio buffer usage elevated: {len(self.audio_buffer)}/{MAX_AUDIO_BUFFER_SIZE} frames "
                    f"({buffer_usage*100:.1f}%)"
                )
            else:
                self.logger.debug(
                    f"Buffered audio frame: {len(data)} bytes "
                    f"(buffer size: {len(self.audio_buffer)})"
                )
        else:
            self.logger.error(
                f"Audio buffer FULL ({len(self.audio_buffer)} frames), "
                f"dropping frame - response may be truncated! "
                f"Consider increasing MAX_AUDIO_BUFFER_SIZE for longer responses."
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
                    "output_modalities": ["text"],
                    "metadata": {"type": "ending_analysis"},
                    "instructions": ENDING_PROMPT
                }
            }

            await self.openai_client._safe_send(json.dumps(ending_prompt))
            summary = None
            try:
                data = await self.openai_client.await_summary(timeout=10)
                if data:
                    summary = data.get("response", {}).get("output", [{}])[0].get("text")
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
        # Generate a summary before closing
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

            self.logger.info(f"[FunctionCall] Initiating server-side disconnect. reason={reason}, info={info}")

            # Generate summary before disconnecting
            summary_data = await self.generate_session_summary()

            # Get token usage from OpenAI client's last response if available
            token_metrics = {}
            if self.openai_client and hasattr(self.openai_client, 'last_response') and self.openai_client.last_response:
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
                self.logger.info(f"[FunctionCall] Token usage: {token_metrics}")

            output_vars = {
                "CONVERSATION_SUMMARY": json.dumps(summary_data) if summary_data else "",
                "CONVERSATION_DURATION": str(time.time() - self.start_time),
                **token_metrics
            }
            outcome = self.session_outcome or {}
            output_vars.update({
                "ESCALATION_REQUIRED": "true" if outcome.get("escalation_required") else "false",
                "ESCALATION_REASON": outcome.get("escalation_reason", ""),
                "COMPLETION_SUMMARY": outcome.get("completion_summary", "")
            })

            disconnect_msg = {
                "version": "2",
                "type": "disconnect",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "reason": reason,
                    "info": info,
                    "outputVariables": output_vars
                }
            }
            self.server_seq += 1

            self.logger.info(f"[FunctionCall] Sending disconnect message to Genesys with {len(output_vars)} output variables")
            await asyncio.wait_for(self._send_json(disconnect_msg), timeout=5.0)

            try:
                await asyncio.wait_for(self.ws.wait_closed(), timeout=5.0)
                self.logger.info(f"[FunctionCall] Genesys acknowledged disconnect for session {self.session_id}")
            except asyncio.TimeoutError:
                logger.warning(f"[FunctionCall] Timeout waiting for Genesys to acknowledge disconnect for session {self.session_id}")
        except Exception as e:
            logger.error(f"[FunctionCall] Error in disconnect_session: {e}")
        finally:
            self.running = False
            await self.stop_audio_processing()
            if self.openai_client:
                await self.openai_client.close()

    async def handle_audio_frame(self, frame_bytes: bytes):
        """
        Processes an incoming audio frame and sends it to the OpenAI client for processing
        if the OpenAI client is available and running.

        If the OpenAI client is not available or not running, the function returns
        immediately without processing the frame. The method increments the frame
        counter upon receiving a frame and logs the amount of data received.

        The method performs asynchronous communication with the OpenAI client to
        deliver the processed audio frame for real-time analysis.

        :param frame_bytes: Audio frame data in bytes.
        :type frame_bytes: bytes
        :return: None
        :rtype: None
        """
        # TODO: Refactor to use and on both tests
        # TODO: self.openai_client and self.openai_client.running:
        if not self.openai_client or not self.openai_client.running:
            return

        # Increment counter to keep track of frames sent
        self.audio_frames_received += 1
        self.logger.debug(f"Received audio frame from Genesys: {len(frame_bytes)} bytes (frame #{self.audio_frames_received})")

        # Send audio frame to OpenAI client real-time model
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
