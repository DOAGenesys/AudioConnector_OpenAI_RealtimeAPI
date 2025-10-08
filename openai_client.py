
import asyncio
import json
import time
import base64
import websockets

from config import (
    logger,
    OPENAI_API_KEY,
    OPENAI_REALTIME_URL,
    RATE_LIMIT_MAX_RETRIES,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEBUG,
    OPENAI_MODEL,
    GENESYS_RATE_WINDOW
)
from utils import format_json, create_final_system_prompt

class OpenAIRealtimeClient:
    def __init__(self, session_id: str, on_speech_started_callback=None):
        self.ws = None
        self.running = False
        self.read_task = None
        self._lock = asyncio.Lock()
        self.session_id = session_id
        self.logger = logger.getChild(f"OpenAIClient_{session_id}")
        self.start_time = time.time()
        self.voice = None
        self.agent_name = None
        self.company_name = None
        self.admin_instructions = None
        self.final_instructions = None
        self.on_speech_started_callback = on_speech_started_callback
        self.retry_count = 0
        self.last_retry_time = 0
        self.rate_limit_delays = {}
        self.last_response = None
        self._summary_future = None
        self.on_end_call_request = None
        self.on_handoff_request = None
        self._await_disconnect_on_done = False
        self._disconnect_context = None

    async def terminate_session(self, reason="completed", final_message=None):
        try:
            if final_message:
                # Send a final message before closing
                event = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": final_message
                            }
                        ]
                    }
                }
                await self._safe_send(json.dumps(event))

            # Send session termination event
            event = {
                "type": "session.update",
                "session": {
                    "status": "completed",
                    "status_details": {"reason": reason}
                }
            }
            await self._safe_send(json.dumps(event))
            
            await self.close()
        except Exception as e:
            self.logger.error(f"Error terminating session: {e}")
            raise   

    async def handle_rate_limit(self):
        if self.retry_count >= RATE_LIMIT_MAX_RETRIES:
            self.logger.error(
                f"[Rate Limit] Max retry attempts ({RATE_LIMIT_MAX_RETRIES}) reached. "
                f"Total duration: {time.time() - self.start_time:.2f}s, "
                f"Last retry at: {self.last_retry_time:.2f}s"
            )
            await self.disconnect_session(reason="error", info="Rate limit max retries exceeded")
            return False

        self.retry_count += 1
        session_duration = time.time() - self.start_time
        self.logger.info(f"[Rate Limit] Current session duration: {session_duration:.2f}s")

        # Align with Genesys rate limits
        if 'Retry-After' in getattr(self.ws, 'response_headers', {}):
            delay = float(self.ws.response_headers['Retry-After'])
        else:
            # Use Genesys default rate window if no specific delay provided
            delay = GENESYS_RATE_WINDOW

        self.logger.warning(
            f"[Rate Limit] Hit rate limit, attempt {self.retry_count}/{RATE_LIMIT_MAX_RETRIES}. "
            f"Backing off for {delay}s. Session duration: {session_duration:.2f}s. "
            f"Time since last retry: {time.time() - self.last_retry_time:.2f}s"
        )

        self.running = False
        self.logger.info("[Rate Limit] Paused operations, starting backoff sleep")
        await asyncio.sleep(delay)
        self.running = True
        self.logger.info("[Rate Limit] Resumed operations after backoff")

        time_since_last = time.time() - self.last_retry_time
        if time_since_last > GENESYS_RATE_WINDOW:
            self.retry_count = 0
            self.logger.info(
                f"[Rate Limit] Reset retry count after {time_since_last:.2f}s "
                f"(window: {GENESYS_RATE_WINDOW}s)"
            )

        self.last_retry_time = time.time()
        return True

    async def connect(self, instructions=None, voice=None, temperature=None, model=None, max_output_tokens=None, agent_name=None, company_name=None):
        ws_connect = websockets.connect

        self.admin_instructions = instructions

        customer_data = getattr(self, 'customer_data', None)
        language = getattr(self, 'language', None)

        self.agent_name = agent_name
        self.company_name = company_name

        self.final_instructions = create_final_system_prompt(
            self.admin_instructions, 
            language=language,
            customer_data=customer_data,
            agent_name=self.agent_name,
            company_name=self.company_name
        )
        self.voice = voice if voice and voice.strip() else "echo"

        try:
            self.temperature = float(temperature) if temperature else DEFAULT_TEMPERATURE
            if not (0.6 <= self.temperature <= 1.2):
                logger.warning(f"Temperature {self.temperature} out of range [0.6, 1.2]. Using default: {DEFAULT_TEMPERATURE}")
                self.temperature = DEFAULT_TEMPERATURE
        except (TypeError, ValueError):
            logger.warning(f"Invalid temperature value: {temperature}. Using default: {DEFAULT_TEMPERATURE}")
            self.temperature = DEFAULT_TEMPERATURE

        self.model = model if model else OPENAI_MODEL
        global OPENAI_REALTIME_URL
        OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={self.model}"

        self.max_output_tokens = max_output_tokens if max_output_tokens else DEFAULT_MAX_OUTPUT_TOKENS

        ws_headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }

        while True:
            try:
                self.logger.info(f"Connecting to OpenAI Realtime API WebSocket using model: {self.model}...")
                connect_start = time.time()

                self.ws = await asyncio.wait_for(
                    ws_connect(
                        OPENAI_REALTIME_URL,
                        extra_headers=ws_headers,
                        max_size=2**23,
                        compression=None,
                        max_queue=32
                    ),
                    timeout=10.0
                )

                connect_time = time.time() - connect_start
                self.logger.info(f"OpenAI WebSocket connection established in {connect_time:.2f}s")
                self.running = True

                msg = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                server_event = json.loads(msg)

                if server_event.get("type") == "error":
                    error_code = server_event.get("code")
                    if error_code == 429:
                        self.logger.warning(
                            f"[Rate Limit] Received 429 during connection. "
                            f"Message: {server_event.get('message', 'No message')}. "
                            f"Session: {self.session_id}"
                        )
                        if await self.handle_rate_limit():
                            await self.close()
                            continue
                        else:
                            await self.close()
                            raise RuntimeError("[Rate Limit] Max rate limit retries exceeded during connection")
                    else:
                        self.logger.error(f"Received error from OpenAI: {server_event}")
                        await self.close()
                        raise RuntimeError(f"OpenAI error: {server_event.get('message', 'Unknown error')}")

                if server_event.get("type") != "session.created":
                    self.logger.error("Did not receive session.created event.")
                    await self.close()
                    raise RuntimeError("OpenAI session not created")

                session_update = {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": self.model,
                        "instructions": self.final_instructions,
                        "output_modalities": ["audio"],
                        "tools": [
                            {
                                "type": "function",
                                "name": "end_call",
                                "description": "End the phone call when the user's request is complete or they say they are done.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "reason": {"type": "string", "description": "Short reason for ending the call"},
                                        "note": {"type": "string", "description": "Optional additional notes"}
                                    }
                                }
                            },
                            {
                                "type": "function",
                                "name": "handoff_to_human",
                                "description": "Escalate the call to a human agent when requested by the caller.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "reason": {"type": "string", "description": "Why the caller wants a human"},
                                        "department": {"type": "string", "description": "Target department or queue"}
                                    }
                                }
                            }
                        ],
                        "tool_choice": "auto",
                        "audio": {
                            "input": {
                                "format": {
                                    "type": "audio/pcmu"
                                },
                                "turn_detection": {
                                    "type": "semantic_vad"
                                }
                            },
                            "output": {
                                "format": {
                                    "type": "audio/pcmu"
                                },
                                "voice": self.voice
                            }
                        }
                    }
                }

                await self._safe_send(json.dumps(session_update))
                try:
                    tool_names = [t.get("name") for t in session_update.get("session", {}).get("tools", [])]
                except Exception:
                    tool_names = []
                self.logger.info(f"[FunctionCall] Configured OpenAI tools: {tool_names}; tool_choice=auto; voice={self.voice}")

                updated_ok = False
                while True:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                    ev = json.loads(msg)
                    self.logger.debug(f"Received after session.update:\n{format_json(ev)}")

                    if ev.get("type") == "error" and ev.get("code") == 429:
                        if await self.handle_rate_limit():
                            await self.close()
                            break
                        else:
                            await self.close()
                            raise RuntimeError("Max rate limit retries exceeded during session update")

                    if ev.get("type") == "session.updated":
                        self.logger.info("[FunctionCall] OpenAI session updated with tools and audio settings")
                        updated_ok = True
                        break

                if not updated_ok:
                    if self.retry_count < RATE_LIMIT_MAX_RETRIES:
                        await self.close()
                        continue
                    else:
                        self.logger.error("Session update not confirmed.")
                        await self.close()
                        raise RuntimeError("OpenAI session update not confirmed")

                self.retry_count = 0
                return

            except (asyncio.TimeoutError, websockets.exceptions.WebSocketException, TypeError) as e:
                self.logger.error(f"Error establishing OpenAI connection: {e}")
                self.logger.error(f"Model: {self.model}")
                self.logger.error(f"URL: {OPENAI_REALTIME_URL}")

                if isinstance(e, websockets.exceptions.WebSocketException):
                    self.logger.error(f"WebSocket specific error details: {str(e)}")
                    if "429" in str(e) and await self.handle_rate_limit():
                        await self.close()
                        continue

                await self.close()
                raise RuntimeError(f"Failed to connect to OpenAI: {str(e)}")

    async def _safe_send(self, message: str):
        async with self._lock:
            if self.ws and self.running:
                try:
                    if DEBUG == 'true':
                        try:
                            msg_dict = json.loads(message)
                            self.logger.debug(f"Sending to OpenAI: type={msg_dict.get('type', 'unknown')}")
                        except json.JSONDecodeError:
                            self.logger.debug("Sending raw message to OpenAI")
                    
                    try:
                        await self.ws.send(message)
                    except websockets.exceptions.WebSocketException as e:
                        if "429" in str(e) and await self.handle_rate_limit():
                            await self.ws.send(message)
                        else:
                            raise
                except Exception as e:
                    self.logger.error(f"Error in _safe_send: {e}")
                    raise

    async def send_audio(self, pcmu_8k: bytes):
        if not self.running or self.ws is None:
            return
        self.logger.debug(f"Sending audio frame to OpenAI: {len(pcmu_8k)} bytes")
        encoded = base64.b64encode(pcmu_8k).decode("utf-8")
        msg = {
            "type": "input_audio_buffer.append",
            "audio": encoded
        }
        await self._safe_send(json.dumps(msg))

    async def start_receiving(self, on_audio_callback):
        if not self.running or not self.ws:
            return

        async def _read_loop():
            try:
                while self.running:
                    raw = await self.ws.recv()
                    try:
                        msg_dict = json.loads(raw)
                        ev_type = msg_dict.get("type", "")

                        if DEBUG == 'true':
                            self.logger.debug(f"Received from OpenAI: type={ev_type}")

                        if ev_type in ("response.audio.delta", "response.output_audio.delta"):
                            delta_b64 = msg_dict.get("delta", "")
                            if delta_b64:
                                pcmu_8k = base64.b64decode(delta_b64)
                                on_audio_callback(pcmu_8k)
                        elif ev_type == "input_audio_buffer.speech_started":
                            if self.on_speech_started_callback:
                                await self.on_speech_started_callback()
                        elif ev_type == "input_audio_buffer.speech_stopped":
                            # End of user turn detected by OpenAI VAD → commit and request response
                            await self._commit_and_request_response()
                        elif ev_type == "response.done":
                            self.last_response = msg_dict.get("response", {})
                            try:
                                meta = msg_dict.get("response", {}).get("metadata", {})
                                if meta.get("type") == "ending_analysis" and self._summary_future and not self._summary_future.done():
                                    self._summary_future.set_result(msg_dict)

                                out = (
                                    msg_dict.get("response", {}).get("output", [])
                                    or msg_dict.get("response", {}).get("content", [])
                                )
                                for item in out:
                                    item_type = item.get("type")
                                    if item_type in ("function_call", "tool_call", "tool", "function"):
                                        name = (
                                            item.get("name")
                                            or (item.get("function") or {}).get("name")
                                        )
                                        call_id = item.get("call_id") or item.get("id")
                                        args_raw = (
                                            item.get("arguments")
                                            or item.get("input")
                                            or item.get("args")
                                            or item.get("parameters")
                                            or (item.get("function") or {}).get("arguments")
                                        )
                                        try:
                                            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                                        except Exception:
                                            args = {}
                                        try:
                                            safe_args_str = json.dumps(args)[:512]
                                        except Exception:
                                            safe_args_str = str(args)[:512]
                                        self.logger.info(f"[FunctionCall] Detected function/tool call: name={name}, call_id={call_id}, args={safe_args_str}")
                                        await self._handle_function_call(name, call_id, args)

                                if self._await_disconnect_on_done and self._disconnect_context:
                                    ctx = self._disconnect_context
                                    self._await_disconnect_on_done = False
                                    self._disconnect_context = None
                                    try:
                                        if ctx.get("action") == "end_call":
                                            if callable(self.on_end_call_request):
                                                await self.on_end_call_request(ctx.get("reason", "completed"), ctx.get("info", ""))
                                        elif ctx.get("action") == "handoff_to_human":
                                            if callable(self.on_handoff_request):
                                                await self.on_handoff_request("transfer", ctx.get("info", "handoff_to_human"))
                                            elif callable(self.on_end_call_request):
                                                await self.on_end_call_request("transfer", ctx.get("info", "handoff_to_human"))
                                    except Exception as e:
                                        self.logger.error(f"Error invoking disconnect callback: {e}")
                                    # Clear input buffer after a completed response/turn
                                    try:
                                        await self._safe_send(json.dumps({"type": "input_audio_buffer.clear"}))
                                    except Exception as e:
                                        self.logger.error(f"Error clearing input buffer: {e}")
                            except Exception:
                                pass
                        elif ev_type == "response.function_call_arguments.delta":
                            # Optional: could stream arguments, but we'll act on response.done
                            pass
                        elif ev_type == "response.created":
                            pass
                    except json.JSONDecodeError:
                        if DEBUG == 'true':
                            self.logger.debug("Received raw message from OpenAI (non-JSON)")
            except websockets.exceptions.ConnectionClosed:
                self.logger.info("OpenAI websocket closed.")
                self.running = False
            except Exception as e:
                self.logger.error(f"Error reading from OpenAI: {e}")
                self.running = False

        self.read_task = asyncio.create_task(_read_loop())

    async def _commit_and_request_response(self):
        try:
            # Finalize current input buffer
            await self._safe_send(json.dumps({"type": "input_audio_buffer.commit"}))
            # Ask model to respond using committed audio
            await self._safe_send(json.dumps({"type": "response.create"}))
        except Exception as e:
            self.logger.error(f"Error committing input buffer and requesting response: {e}")

    async def _handle_function_call(self, name: str, call_id: str, args: dict):
        try:
            self.logger.info(f"[FunctionCall] Handling function call: name={name}, call_id={call_id}")
            output_payload = {}
            action = None
            info = None
            if name == "end_call":
                action = "end_call"
                reason = (args or {}).get("reason") or "User indicated conversation is complete"
                info = (args or {}).get("note") or reason
                output_payload = {"result": "ok", "action": action, "reason": reason}
                # Schedule disconnect after model generates a short closing
                self._disconnect_context = {"action": action, "reason": "completed", "info": info}
                self._await_disconnect_on_done = True
            elif name == "handoff_to_human":
                action = "handoff_to_human"
                reason = (args or {}).get("reason") or "Caller requested human agent"
                department = (args or {}).get("department")
                output_payload = {"result": "ok", "action": action, "reason": reason, "department": department}
                info = reason if not department else f"{reason}. dept={department}"
                self._disconnect_context = {"action": action, "reason": "transfer", "info": info}
                self._await_disconnect_on_done = True
            else:
                output_payload = {"result": "ignored", "reason": "unknown_function"}

            event1 = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output_payload)
                }
            }
            await self._safe_send(json.dumps(event1))
            self.logger.info(f"[FunctionCall] Sent function_call_output for call_id={call_id} payload={json.dumps(output_payload)[:512]}")

            # Ask model to produce a short, final audio message
            event2 = {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "output_modalities": ["audio"],
                    "instructions": "Acknowledge the user's request and say a brief, courteous goodbye in one short sentence.",
                    "metadata": {"type": "final_farewell"}
                }
            }
            await self._safe_send(json.dumps(event2))
            if self._disconnect_context:
                self.logger.info(f"[FunctionCall] Scheduled Genesys disconnect after farewell: action={self._disconnect_context.get('action')}, reason={self._disconnect_context.get('reason')}, info={self._disconnect_context.get('info')}")
        except Exception as e:
            self.logger.error(f"[FunctionCall] Error handling function call {name}: {e}")

    async def close(self):
        duration = time.time() - self.start_time
        self.logger.info(f"Closing OpenAI connection after {duration:.2f}s")
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                self.logger.error(f"Error closing OpenAI connection: {e}")
            self.ws = None
        if self.read_task:
            self.read_task.cancel()
            self.read_task = None

    async def await_summary(self, timeout: float = 10.0):
        loop = asyncio.get_event_loop()
        self._summary_future = loop.create_future()
        try:
            return await asyncio.wait_for(self._summary_future, timeout=timeout)
        finally:
            self._summary_future = None

    async def disconnect_session(self, reason="completed", info=""):
        await self.close()
