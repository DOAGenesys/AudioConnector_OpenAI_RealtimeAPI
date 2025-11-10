
import asyncio
import json
import time
import base64
from typing import Any, Awaitable, Callable, Dict, List, Optional

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
from utils import format_json, create_final_system_prompt, is_websocket_open, get_websocket_connect_kwargs


TERMINATION_GUIDANCE = """[CALL CONTROL]
Call `end_conversation_successfully` when the caller's request has been resolved. Use the `summary` field to explain what was accomplished.
Call `end_conversation_with_escalation` when the caller explicitly requests a human, the task is blocked, or additional assistance is needed. Use the `reason` field to describe why escalation is required.
Always invoke the correct call-control tool as soon as the user's intent is clear. After confirming, deliver a short verbal acknowledgment."""


def _default_call_control_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "end_conversation_successfully",
            "description": (
                "Gracefully end the phone call when the caller confirms their needs are met. "
                "Provide a short summary of the completed task in the `summary` field."
            ),
            "parameters": {
                "type": "object",
                "strict": True,
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-sentence summary of what was accomplished before ending the call."
                    }
                },
                "required": ["summary"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "end_conversation_with_escalation",
            "description": (
                "End the phone call and request a warm transfer to a human agent when the caller asks for a person, is dissatisfied, or the task cannot be completed."
            ),
            "parameters": {
                "type": "object",
                "strict": True,
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why escalation is needed (e.g., customer requested human, policy restriction, unable to authenticate)."
                    }
                },
                "required": ["reason"],
                "additionalProperties": False
            }
        }
    ]

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
        self.custom_tool_definitions: List[Dict[str, Any]] = []
        self.tool_instruction_text: Optional[str] = None
        self.custom_tool_choice: Optional[Any] = None
        self.genesys_tool_handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}
        self._response_in_progress = False
        self._has_audio_in_buffer = False
        self.escalation_prompt = None
        self.success_prompt = None

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

    async def connect(
        self,
        instructions=None,
        voice=None,
        temperature=None,
        model=None,
        max_output_tokens=None,
        agent_name=None,
        company_name=None,
        tool_definitions: Optional[List[Dict[str, Any]]] = None,
        tool_instructions: Optional[str] = None,
        tool_choice: Optional[Any] = None
    ):
        self.admin_instructions = instructions

        customer_data = getattr(self, 'customer_data', None)
        language = getattr(self, 'language', None)

        self.agent_name = agent_name
        self.company_name = company_name
        self.custom_tool_definitions = tool_definitions or []
        self.tool_instruction_text = tool_instructions
        self.custom_tool_choice = tool_choice

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

                # WEBSOCKETS VERSION COMPATIBILITY:
                # Use version-agnostic helper to build connect kwargs
                # websockets < 15.0 uses 'additional_headers', >= 15.0 uses 'extra_headers'
                connect_kwargs = get_websocket_connect_kwargs(
                    OPENAI_REALTIME_URL,
                    ws_headers,
                    max_size=2**23,
                    compression=None,
                    max_queue=32
                )

                self.ws = await asyncio.wait_for(
                    websockets.connect(**connect_kwargs),
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

                instructions_text = self.final_instructions
                extra_blocks = [TERMINATION_GUIDANCE]
                if self.tool_instruction_text:
                    extra_blocks.append(self.tool_instruction_text)
                instructions_text = "\n\n".join([instructions_text] + extra_blocks) if extra_blocks else instructions_text

                tools = _default_call_control_tools()
                if self.custom_tool_definitions:
                    tools.extend(self.custom_tool_definitions)

                session_update = {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": self.model,
                        "instructions": instructions_text,
                        "output_modalities": ["audio"],
                        "tools": tools,
                        "tool_choice": self.custom_tool_choice or "auto",
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
                tools_configured = session_update.get("session", {}).get("tools", []) or []
                tool_descriptors = []
                for tool in tools_configured:
                    if isinstance(tool, dict):
                        descriptor = (
                            tool.get("name")
                            or tool.get("server_label")
                            or tool.get("server_name")
                            or tool.get("type")
                            or "tool"
                        )
                    else:
                        descriptor = str(tool)
                    tool_descriptors.append(descriptor)
                tool_choice_value = session_update.get("session", {}).get("tool_choice") or "auto"
                if isinstance(tool_choice_value, (dict, list)):
                    choice_repr = format_json(tool_choice_value)
                else:
                    choice_repr = tool_choice_value
                self.logger.info(
                    f"[FunctionCall] Configured OpenAI tools: {tool_descriptors}; tool_choice={choice_repr}; voice={self.voice}"
                )

                updated_ok = False
                while True:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                    ev = json.loads(msg)
                    self.logger.info(f"[FunctionCall] Received after session.update:\n{format_json(ev)}")

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
            # WEBSOCKETS VERSION COMPATIBILITY:
            # Use is_websocket_open() helper for backward compatibility with websockets < 15.0
            # Fixes Issue #9 from legacy buglog - Missing WebSocket State Validation
            if self.ws and self.running and is_websocket_open(self.ws):
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
                            # IMPORTANT: Re-validate websocket state after rate limit handling
                            # Fixes Issue #2 from legacy buglog - Race Condition in _safe_send
                            # handle_rate_limit() may close websocket, so must verify before retry
                            if self.ws and self.running and is_websocket_open(self.ws):
                                await self.ws.send(message)
                            else:
                                self.logger.warning("WebSocket not in open state after rate limit handling, skipping retry")
                        else:
                            raise
                except Exception as e:
                    self.logger.error(f"Error in _safe_send: {e}")
                    raise

    async def send_audio(self, pcmu_8k: bytes):
        # WEBSOCKETS VERSION COMPATIBILITY:
        # Use is_websocket_open() for backward compatibility with websockets < 15.0
        # Fixes Issue #11 from legacy buglog - Silent Failure in audio send
        # Now logs warning when dropping audio frames instead of silently returning
        if not self.running or self.ws is None or not is_websocket_open(self.ws):
            if DEBUG == 'true' and self.ws is not None:
                self.logger.warning(f"Dropping audio frame: running={self.running}, ws_open={is_websocket_open(self.ws)}")
            return
        self.logger.debug(f"Sending audio frame to OpenAI: {len(pcmu_8k)} bytes")
        encoded = base64.b64encode(pcmu_8k).decode("utf-8")
        msg = {
            "type": "input_audio_buffer.append",
            "audio": encoded
        }
        await self._safe_send(json.dumps(msg))
        self._has_audio_in_buffer = True

    async def start_receiving(self, on_audio_callback):
        # WEBSOCKETS VERSION COMPATIBILITY:
        # Use is_websocket_open() for backward compatibility with websockets < 15.0
        # Validates websocket is in OPEN state before starting read loop
        if not self.running or not self.ws or not is_websocket_open(self.ws):
            self.logger.warning(f"Cannot start receiving: running={self.running}, ws_exists={self.ws is not None}, ws_open={is_websocket_open(self.ws)}")
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
                            self.logger.info("[FunctionCall] User speech started (VAD detected)")
                            if self.on_speech_started_callback:
                                await self.on_speech_started_callback()
                        elif ev_type == "input_audio_buffer.speech_stopped":
                            self.logger.info("[FunctionCall] User speech stopped (VAD detected)")
                            await self._commit_and_request_response()
                        elif ev_type == "input_audio_buffer.committed":
                            self._has_audio_in_buffer = False
                        elif ev_type == "input_audio_buffer.cleared":
                            self._has_audio_in_buffer = False
                        elif ev_type == "response.created":
                            self._response_in_progress = True
                            response_id = msg_dict.get("response", {}).get("id", "unknown")
                            self.logger.info(f"[FunctionCall] OpenAI started generating response id={response_id}")
                        elif ev_type == "response.done":
                            self._response_in_progress = False
                            self.last_response = msg_dict.get("response", {})
                            try:
                                response_obj = msg_dict.get("response", {})
                                response_id = response_obj.get("id", "unknown")
                                response_status = response_obj.get("status", "unknown")
                                
                                out = (
                                    response_obj.get("output", [])
                                    or response_obj.get("content", [])
                                )
                                
                                output_summary = []
                                for item in out:
                                    item_type = item.get("type", "unknown")
                                    if item_type in ("function_call", "tool_call", "tool", "function"):
                                        tool_name = item.get("name") or (item.get("function") or {}).get("name") or "unknown"
                                        output_summary.append(f"function_call:{tool_name}")
                                    elif item_type == "message":
                                        content_items = item.get("content", [])
                                        for c in content_items:
                                            c_type = c.get("type", "unknown")
                                            if c_type == "text":
                                                text_preview = (c.get("text") or "")[:100]
                                                output_summary.append(f"text:{text_preview}")
                                            elif c_type == "audio":
                                                output_summary.append("audio")
                                            else:
                                                output_summary.append(c_type)
                                    else:
                                        output_summary.append(item_type)
                                
                                summary_str = ", ".join(output_summary) if output_summary else "no output"
                                self.logger.info(f"[FunctionCall] response.done id={response_id}, status={response_status}, output=[{summary_str}]")
                                
                                meta = response_obj.get("metadata") or {}
                                if meta.get("type") == "ending_analysis" and self._summary_future and not self._summary_future.done():
                                    self._summary_future.set_result(msg_dict)

                                for item in out:
                                    item_type = item.get("type")
                                    if item_type in ("function_call", "tool_call", "tool", "function"):
                                        try:
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
                                            except json.JSONDecodeError as json_err:
                                                self.logger.error(f"[FunctionCall] ERROR: Failed to parse function arguments: {json_err}. Raw args: {args_raw[:200]}")
                                                args = {}
                                            except Exception as parse_err:
                                                self.logger.error(f"[FunctionCall] ERROR: Unexpected error parsing arguments: {parse_err}", exc_info=True)
                                                args = {}
                                            
                                            try:
                                                safe_args_str = json.dumps(args)[:512]
                                            except Exception:
                                                safe_args_str = str(args)[:512]
                                            
                                            self.logger.info(f"[FunctionCall] Detected function/tool call: name={name}, call_id={call_id}, args={safe_args_str}")
                                            await self._handle_function_call(name, call_id, args)
                                        except Exception as call_err:
                                            self.logger.error(f"[FunctionCall] ERROR: Failed to process function call from response.done: {call_err}", exc_info=True)

                                if self._await_disconnect_on_done and self._disconnect_context:
                                    ctx = self._disconnect_context
                                    self._await_disconnect_on_done = False
                                    self._disconnect_context = None
                                    try:
                                        if ctx.get("action") == "end_conversation_successfully":
                                            if callable(self.on_end_call_request):
                                                await self.on_end_call_request(ctx.get("reason", "completed"), ctx.get("info", ""))
                                        elif ctx.get("action") == "end_conversation_with_escalation":
                                            if callable(self.on_handoff_request):
                                                await self.on_handoff_request("transfer", ctx.get("info", ""))
                                            elif callable(self.on_end_call_request):
                                                await self.on_end_call_request("transfer", ctx.get("info", ""))
                                    except Exception as e:
                                        self.logger.error(f"[FunctionCall] ERROR: Exception invoking disconnect callback: {e}", exc_info=True)
                                    try:
                                        await self._safe_send(json.dumps({"type": "input_audio_buffer.clear"}))
                                    except Exception as e:
                                        self.logger.error(f"[FunctionCall] ERROR: Failed to clear input buffer: {e}", exc_info=True)
                            except Exception as response_err:
                                self.logger.error(f"[FunctionCall] ERROR: Unexpected error processing response.done event: {response_err}", exc_info=True)
                        elif ev_type == "error":
                            error_code = msg_dict.get("code")
                            error_message = msg_dict.get("message", "No error message provided")
                            error_type = msg_dict.get("error", {}).get("type") if isinstance(msg_dict.get("error"), dict) else None
                            error_code_str = msg_dict.get("error", {}).get("code") if isinstance(msg_dict.get("error"), dict) else None
                            error_details = format_json(msg_dict)
                            
                            if error_code_str == "input_audio_buffer_commit_empty":
                                self.logger.debug(
                                    f"[OpenAI] Attempted to commit empty audio buffer - "
                                    f"this is now prevented by buffer state tracking"
                                )
                                self._has_audio_in_buffer = False
                            elif error_code_str == "conversation_already_has_active_response":
                                self.logger.debug(
                                    f"[OpenAI] Attempted to create response while one is in progress - "
                                    f"this is now prevented by response state tracking"
                                )
                                self._response_in_progress = True
                            elif error_code == 429:
                                self.logger.error(
                                    f"[OpenAI Error] Code: {error_code}, Message: {error_message}, "
                                    f"Type: {error_type}, Full details: {error_details}"
                                )
                                if await self.handle_rate_limit():
                                    await self.close()
                                else:
                                    self.logger.error("[OpenAI Error] Rate limit exceeded and max retries reached")
                            else:
                                self.logger.error(
                                    f"[OpenAI Error] Code: {error_code}, Message: {error_message}, "
                                    f"Type: {error_type}, Full details: {error_details}"
                                )
                        elif ev_type == "response.function_call_arguments.delta":
                            pass
                        elif ev_type.startswith("response.mcp_call"):
                            self._handle_mcp_server_event(msg_dict)
                        elif ev_type.startswith("mcp_list_tools"):
                            self._handle_mcp_list_event(msg_dict)
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
            if self._response_in_progress:
                self.logger.debug("Skipping commit/response request: response already in progress")
                return
            
            if not self._has_audio_in_buffer:
                self.logger.debug("Skipping commit/response request: no audio in buffer")
                return
            
            self.logger.info("[FunctionCall] User speech ended, committing audio buffer and requesting OpenAI response")
            await self._safe_send(json.dumps({"type": "input_audio_buffer.commit"}))
            await self._safe_send(json.dumps({"type": "response.create"}))
        except Exception as e:
            self.logger.error(f"Error committing input buffer and requesting response: {e}")

    async def _handle_function_call(self, name: str, call_id: str, args: dict):
        try:
            self.logger.info(f"[FunctionCall] Handling function call: name={name}, call_id={call_id}")
            
            if not name:
                self.logger.error(f"[FunctionCall] ERROR: Function name is empty or None for call_id={call_id}")
                await self._send_error_to_openai(call_id, "Function name is missing")
                return
            
            if not call_id:
                self.logger.error(f"[FunctionCall] ERROR: call_id is empty or None for function={name}")
                return
            
            if name in self.genesys_tool_handlers:
                await self._handle_genesys_tool_call(name, call_id, args or {})
                return

            output_payload = {}
            action = None
            info = None
            closing_instruction = None
            if name in ("end_call", "end_conversation_successfully"):
                action = "end_conversation_successfully"
                summary = (args or {}).get("summary") or (args or {}).get("note") or "Customer confirmed the request was completed."
                info = summary
                output_payload = {"result": "ok", "action": action, "summary": summary}
                self._disconnect_context = {"action": action, "reason": "completed", "info": info}
                self._await_disconnect_on_done = True
                # Use custom SUCCESS_PROMPT if provided, otherwise use default
                if self.success_prompt:
                    closing_instruction = f'Say exactly this to the caller: "{self.success_prompt}"'
                    self.logger.info(f"[FunctionCall] Using custom SUCCESS_PROMPT for closing: {self.success_prompt}")
                else:
                    closing_instruction = "Confirm the task is wrapped up and thank the caller in one short sentence."
            elif name in ("handoff_to_human", "end_conversation_with_escalation"):
                action = "end_conversation_with_escalation"
                reason = (args or {}).get("reason") or "Caller requested escalation"
                output_payload = {"result": "ok", "action": action, "reason": reason}
                info = reason
                self._disconnect_context = {"action": action, "reason": "transfer", "info": info}
                self._await_disconnect_on_done = True
                # Use custom ESCALATION_PROMPT if provided, otherwise use default
                if self.escalation_prompt:
                    closing_instruction = f'Say exactly this to the caller: "{self.escalation_prompt}"'
                    self.logger.info(f"[FunctionCall] Using custom ESCALATION_PROMPT for closing: {self.escalation_prompt}")
                else:
                    closing_instruction = "Let the caller know a live agent will take over and reassure them help is coming."
            else:
                self.logger.warning(f"[FunctionCall] Unknown function called: {name}. Sending error response.")
                output_payload = {"result": "error", "error": f"Unknown function: {name}"}

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

            if closing_instruction:
                event2 = {
                    "type": "response.create",
                    "response": {
                        "conversation": "none",
                        "output_modalities": ["audio"],
                        "instructions": closing_instruction,
                        "metadata": {"type": "final_farewell"}
                    }
                }
                await self._safe_send(json.dumps(event2))
                if self._disconnect_context:
                    self.logger.info(
                        f"[FunctionCall] Scheduled Genesys disconnect after farewell: action={self._disconnect_context.get('action')}, reason={self._disconnect_context.get('reason')}, info={self._disconnect_context.get('info')}"
                    )
        except json.JSONEncodeError as e:
            self.logger.error(f"[FunctionCall] ERROR: JSON encoding failed for function {name}, call_id={call_id}: {e}", exc_info=True)
            await self._send_error_to_openai(call_id, f"JSON encoding error: {str(e)}")
        except Exception as e:
            self.logger.error(f"[FunctionCall] ERROR: Exception handling function call {name}, call_id={call_id}: {e}", exc_info=True)
            await self._send_error_to_openai(call_id, f"Internal error: {str(e)}")

    def register_genesys_tool_handlers(self, handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]]):
        self.genesys_tool_handlers = handlers or {}

    async def _handle_genesys_tool_call(self, name: str, call_id: str, args: Dict[str, Any]):
        handler = self.genesys_tool_handlers.get(name)
        if not handler:
            error_msg = f"No handler registered for tool {name}"
            self.logger.error(f"[FunctionCall] ERROR: {error_msg}")
            await self._send_error_to_openai(call_id, error_msg)
            return
        
        try:
            self.logger.info(f"[FunctionCall] Validating arguments for tool {name}")
            if not isinstance(args, dict):
                raise ValueError(f"Tool arguments must be a dictionary, got {type(args).__name__}")
            
            try:
                args_preview = json.dumps(args)[:512]
            except Exception:
                args_preview = str(args)[:512]
            self.logger.info(f"[FunctionCall] Calling handler for tool {name} with args: {args_preview}")
            
            result_payload = await handler(args)
            
            if result_payload is None:
                self.logger.warning(f"[FunctionCall] WARNING: Tool {name} returned None. Treating as empty result.")
                result_payload = {}
            
            output_payload = {
                "status": "ok",
                "tool": name,
                "result": result_payload
            }
            try:
                result_preview = json.dumps(result_payload)[:1024]
            except Exception:
                result_preview = str(result_payload)[:1024]
            self.logger.info(f"[FunctionCall] Genesys tool {name} executed successfully. Result preview: {result_preview}")
            
        except ValueError as exc:
            error_msg = f"Validation error: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} validation failed: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": "validation_error",
                "message": error_msg
            }
        except TimeoutError as exc:
            error_msg = f"Tool execution timeout: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} timed out: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": "timeout",
                "message": error_msg
            }
        except ConnectionError as exc:
            error_msg = f"Connection error: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} connection failed: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": "connection_error",
                "message": error_msg
            }
        except json.JSONDecodeError as exc:
            error_msg = f"JSON parsing error: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} JSON error: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": "json_error",
                "message": error_msg
            }
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} failed with unexpected exception: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": type(exc).__name__,
                "message": error_msg
            }

        try:
            await self._send_function_output(call_id, output_payload)
            self.logger.info(f"[FunctionCall] Requesting OpenAI to process tool result for call_id={call_id}")
            await self._safe_send(json.dumps({"type": "response.create"}))
        except Exception as send_exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send tool result to OpenAI for call_id={call_id}: {send_exc}", exc_info=True)

    async def _send_function_output(self, call_id: str, payload: Dict[str, Any]):
        try:
            if not call_id:
                self.logger.error(f"[FunctionCall] ERROR: Cannot send function output - call_id is empty")
                return
            
            if not isinstance(payload, dict):
                self.logger.error(f"[FunctionCall] ERROR: Payload must be a dictionary, got {type(payload).__name__}")
                return
            
            try:
                output_str = json.dumps(payload)
            except (TypeError, ValueError) as json_err:
                self.logger.error(f"[FunctionCall] ERROR: Failed to serialize payload to JSON: {json_err}", exc_info=True)
                error_payload = {
                    "status": "error",
                    "error_type": "serialization_error",
                    "message": f"Failed to serialize result: {str(json_err)}"
                }
                output_str = json.dumps(error_payload)
            
            event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_str
                }
            }
            preview = output_str[:1024] if len(output_str) > 1024 else output_str
            self.logger.info(f"[FunctionCall] Sending function output to OpenAI for call_id={call_id}. Output preview: {preview}")
            
            await self._safe_send(json.dumps(event))
            self.logger.info(f"[FunctionCall] Successfully sent function output to OpenAI for call_id={call_id}")
        except Exception as exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send function output for {call_id}: {exc}", exc_info=True)
    
    async def _send_error_to_openai(self, call_id: str, error_message: str):
        try:
            if not call_id:
                self.logger.error(f"[FunctionCall] ERROR: Cannot send error - call_id is empty. Error was: {error_message}")
                return
            
            error_payload = {
                "status": "error",
                "message": error_message,
                "timestamp": time.time()
            }
            
            event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(error_payload)
                }
            }
            
            self.logger.info(f"[FunctionCall] Sending error to OpenAI for call_id={call_id}: {error_message}")
            await self._safe_send(json.dumps(event))
            
            await self._safe_send(json.dumps({"type": "response.create"}))
            self.logger.info(f"[FunctionCall] Error sent and response requested for call_id={call_id}")
        except Exception as exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send error to OpenAI for call_id={call_id}: {exc}", exc_info=True)

    def _handle_mcp_server_event(self, event: Dict[str, Any]):
        ev_type = event.get("type", "")
        item_id = event.get("item_id")
        call_id = event.get("call_id")
        if ev_type.endswith("arguments.delta"):
            delta = event.get("delta", "")
            preview = delta if isinstance(delta, str) else json.dumps(delta)
            self.logger.info(f"[MCP] arguments.delta item={item_id} call_id={call_id}: {preview[:256]}")
        elif ev_type.endswith("arguments.done"):
            args = event.get("arguments", "")
            preview = args if isinstance(args, str) else json.dumps(args)
            self.logger.info(f"[MCP] arguments.done item={item_id} call_id={call_id}: {preview[:256]}")
        elif ev_type.endswith(".in_progress"):
            self.logger.info(f"[MCP] Tool call in progress item={item_id} call_id={call_id}")
        elif ev_type.endswith(".completed"):
            self.logger.info(f"[MCP] Tool call completed item={item_id} call_id={call_id}")
        elif ev_type.endswith(".failed"):
            message = event.get("error") or event.get("message") or format_json(event)
            self.logger.error(f"[MCP] Tool call failed item={item_id} call_id={call_id}: {str(message)[:256]}")

    def _handle_mcp_list_event(self, event: Dict[str, Any]):
        ev_type = event.get("type", "")
        item_id = event.get("item_id")
        if ev_type.endswith(".completed"):
            self.logger.info(f"[MCP] mcp.list_tools completed for item={item_id}")
        elif ev_type.endswith(".failed"):
            message = event.get("error") or event.get("message") or format_json(event)
            self.logger.warning(f"[MCP] mcp.list_tools failed for item={item_id}: {str(message)[:256]}")
        else:
            self.logger.info(f"[MCP] mcp.list_tools.{ev_type.split('.')[-1]} item={item_id}")

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
