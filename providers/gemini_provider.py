"""
Gemini Live API provider implementation.

Provides full feature parity with OpenAI Realtime API including:
- Real-time audio streaming (PCM16 16kHz input, 24kHz output)
- Voice Activity Detection (VAD)
- Function calling / tool use
- Session management
- Audio transcriptions
"""

import asyncio
import json
import time
import base64
import copy
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets

from config import (
    logger,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_BASE_URL,
    GEMINI_API_VERSION,
    RATE_LIMIT_MAX_RETRIES,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEBUG,
    GENESYS_RATE_WINDOW
)
from utils import format_json, create_final_system_prompt, is_websocket_open, pcmu_8k_to_pcm16_16k, pcm16_24k_to_pcmu_8k


TERMINATION_GUIDANCE = """[CALL CONTROL]
Call `end_conversation_successfully` when the caller's request has been resolved. Use the `summary` field to explain what was accomplished.
Call `end_conversation_with_escalation` when the caller explicitly requests a human, the task is blocked, or additional assistance is needed. Use the `reason` field to describe why escalation is required.
Always invoke the correct call-control tool as soon as the user's intent is clear. After confirming, deliver a short verbal acknowledgment."""


def _default_call_control_tools() -> List[Dict[str, Any]]:
    """
    Build Gemini-format call control function declarations.
    Maps to same semantic actions as OpenAI but uses Gemini's schema format.
    """
    return [
        {
            "name": "end_conversation_successfully",
            "description": (
                "Gracefully end the phone call when the caller confirms their needs are met. "
                "Provide a short summary of the completed task in the `summary` field."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-sentence summary of what was accomplished before ending the call."
                    }
                },
                "required": ["summary"]
            }
        },
        {
            "name": "end_conversation_with_escalation",
            "description": (
                "End the phone call and request a warm transfer to a human agent when the caller asks for a person, is dissatisfied, or the task cannot be completed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why escalation is needed (e.g., customer requested human, policy restriction, unable to authenticate)."
                    }
                },
                "required": ["reason"]
            }
        }
    ]


def _clean_schema_for_gemini(schema: Any) -> Any:
    """
    Recursively remove OpenAI-specific fields from a schema.

    Removes:
    - strict: OpenAI-specific structured output parameter
    - additionalProperties: While JSON Schema standard, Gemini doesn't accept it
    """
    if not isinstance(schema, dict):
        return schema

    # Create a deep copy to avoid modifying the original
    cleaned = copy.deepcopy(schema)

    # Remove OpenAI-specific fields at this level
    cleaned.pop("strict", None)
    cleaned.pop("additionalProperties", None)

    # Recursively clean nested objects
    if "properties" in cleaned and isinstance(cleaned["properties"], dict):
        for key, value in cleaned["properties"].items():
            if isinstance(value, dict):
                cleaned["properties"][key] = _clean_schema_for_gemini(value)

    if "items" in cleaned and isinstance(cleaned["items"], dict):
        cleaned["items"] = _clean_schema_for_gemini(cleaned["items"])

    if "definitions" in cleaned and isinstance(cleaned["definitions"], dict):
        for key, value in cleaned["definitions"].items():
            if isinstance(value, dict):
                cleaned["definitions"][key] = _clean_schema_for_gemini(value)

    return cleaned


def _convert_openai_tool_to_gemini(openai_tool: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert OpenAI function definition format to Gemini function declaration format.

    OpenAI format:
    {
        "type": "function",
        "name": "...",
        "description": "...",
        "parameters": {"type": "object", "properties": {...}, "required": [...], "strict": True}
    }

    Gemini format:
    {
        "name": "...",
        "description": "...",
        "parameters": {"type": "object", "properties": {...}, "required": [...]}
    }

    Note: Removes OpenAI-specific fields like 'strict' and 'additionalProperties' that Gemini rejects.
    """
    # Get the parameters and clean them recursively
    parameters = openai_tool.get("parameters", {})
    cleaned_parameters = _clean_schema_for_gemini(parameters)

    gemini_tool = {
        "name": openai_tool.get("name"),
        "description": openai_tool.get("description", ""),
        "parameters": cleaned_parameters
    }

    return gemini_tool


class GeminiLiveClient:
    """
    Gemini Live API client with feature parity to OpenAI Realtime API.
    """

    def __init__(self, session_id: str, on_speech_started_callback=None):
        self.ws = None
        self.running = False
        self.read_task = None
        self._lock = asyncio.Lock()
        self.session_id = session_id
        self.logger = logger.getChild(f"GeminiClient_{session_id}")
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

        # Gemini-specific state
        self._pending_function_calls: Dict[str, Dict[str, Any]] = {}
        self._turn_complete = False

    async def terminate_session(self, reason="completed", final_message=None):
        """
        Terminate the Gemini session gracefully.
        """
        try:
            if final_message:
                # Send final text message
                await self._send_client_content(text=final_message, turn_complete=True)

            await self.close()
        except Exception as e:
            self.logger.error(f"Error terminating session: {e}")
            raise

    async def handle_rate_limit(self):
        """
        Handle rate limiting with exponential backoff.
        """
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
        """
        Establish connection to Gemini Live API.
        """
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

        # Gemini voice mapping (use closest match to OpenAI voices)
        # Gemini voices: Puck, Charon, Kore, Fenrir, Aoede
        voice_mapping = {
            "alloy": "Puck",
            "ash": "Charon",
            "ballad": "Aoede",
            "coral": "Kore",
            "echo": "Puck",
            "sage": "Fenrir",
            "shimmer": "Aoede",
            "verse": "Charon"
        }
        self.voice = voice_mapping.get(voice, "Puck") if voice and voice.strip() else "Puck"

        try:
            self.temperature = float(temperature) if temperature else DEFAULT_TEMPERATURE
            # Gemini supports 0.0-2.0 temperature range
            if not (0.0 <= self.temperature <= 2.0):
                logger.warning(f"Temperature {self.temperature} out of Gemini's range [0.0, 2.0]. Using default: {DEFAULT_TEMPERATURE}")
                self.temperature = DEFAULT_TEMPERATURE
        except (TypeError, ValueError):
            logger.warning(f"Invalid temperature value: {temperature}. Using default: {DEFAULT_TEMPERATURE}")
            self.temperature = DEFAULT_TEMPERATURE

        self.model = model if model else GEMINI_MODEL

        # Build WebSocket URL for Gemini Live API
        ws_url = f"wss://{GEMINI_BASE_URL}/{GEMINI_API_VERSION}/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

        while True:
            try:
                self.logger.info(f"Connecting to Gemini Live API WebSocket using model: {self.model}...")
                connect_start = time.time()

                # Gemini doesn't use additional headers for authentication (uses query param)
                self.ws = await asyncio.wait_for(
                    websockets.connect(ws_url, max_size=2**23, compression=None, max_queue=32),
                    timeout=10.0
                )

                connect_time = time.time() - connect_start
                self.logger.info(f"Gemini WebSocket connection established in {connect_time:.2f}s")
                self.running = True

                # Build system instructions
                instructions_text = self.final_instructions
                extra_blocks = [TERMINATION_GUIDANCE]
                if self.tool_instruction_text:
                    extra_blocks.append(self.tool_instruction_text)
                instructions_text = "\n\n".join([instructions_text] + extra_blocks) if extra_blocks else instructions_text

                # Build tools list (Gemini format)
                tools = []
                call_control_tools = _default_call_control_tools()
                for tool in call_control_tools:
                    tools.append(tool)

                if self.custom_tool_definitions:
                    for openai_tool in self.custom_tool_definitions:
                        gemini_tool = _convert_openai_tool_to_gemini(openai_tool)
                        tools.append(gemini_tool)

                # Build function_declarations wrapper
                function_declarations = tools if tools else []

                # Send setup message to configure the session
                setup_message = {
                    "setup": {
                        "model": f"models/{self.model}",
                        "generation_config": {
                            "response_modalities": ["AUDIO"],
                            "speech_config": {
                                "voice_config": {
                                    "prebuilt_voice_config": {
                                        "voice_name": self.voice
                                    }
                                }
                            }
                        }
                    }
                }

                # Add system instruction if present
                if instructions_text:
                    setup_message["setup"]["system_instruction"] = {
                        "parts": [{"text": instructions_text}]
                    }

                # Add tools if present
                if function_declarations:
                    setup_message["setup"]["tools"] = [
                        {"function_declarations": function_declarations}
                    ]

                await self._safe_send(json.dumps(setup_message))

                tool_names = [t.get("name", "unknown") for t in function_declarations]
                self.logger.info(
                    f"[FunctionCall] Configured Gemini tools: {tool_names}; voice={self.voice}"
                )

                # Wait for setup complete response
                setup_response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                setup_data = json.loads(setup_response)

                if "setupComplete" in setup_data:
                    self.logger.info("[FunctionCall] Gemini session setup completed successfully")
                    self.retry_count = 0
                    return
                elif "error" in setup_data:
                    error_msg = setup_data.get("error", {})
                    self.logger.error(f"Gemini setup error: {error_msg}")
                    raise RuntimeError(f"Gemini setup error: {error_msg}")
                else:
                    self.logger.warning(f"Unexpected setup response: {setup_data}")

            except (asyncio.TimeoutError, websockets.exceptions.WebSocketException, TypeError) as e:
                self.logger.error(f"Error establishing Gemini connection: {e}")
                self.logger.error(f"Model: {self.model}")
                self.logger.error(f"URL: {ws_url[:100]}...")

                if isinstance(e, websockets.exceptions.WebSocketException):
                    self.logger.error(f"WebSocket specific error details: {str(e)}")
                    if "429" in str(e) and await self.handle_rate_limit():
                        await self.close()
                        continue

                await self.close()
                raise RuntimeError(f"Failed to connect to Gemini: {str(e)}")

    async def _safe_send(self, message: str):
        """
        Thread-safe WebSocket message sending.
        """
        async with self._lock:
            if self.ws and self.running and is_websocket_open(self.ws):
                try:
                    if DEBUG == 'true':
                        try:
                            msg_dict = json.loads(message)
                            msg_type = list(msg_dict.keys())[0] if msg_dict else 'unknown'
                            self.logger.debug(f"Sending to Gemini: type={msg_type}")
                        except json.JSONDecodeError:
                            self.logger.debug("Sending raw message to Gemini")

                    await self.ws.send(message)
                except Exception as e:
                    self.logger.error(f"Error in _safe_send: {e}")
                    raise

    async def send_audio(self, pcmu_8k: bytes):
        """
        Send audio to Gemini (converts PCMU 8kHz to PCM16 16kHz).
        """
        if not self.running or self.ws is None or not is_websocket_open(self.ws):
            if DEBUG == 'true' and self.ws is not None:
                self.logger.warning(f"Dropping audio frame: running={self.running}, ws_open={is_websocket_open(self.ws)}")
            return

        # Convert PCMU 8kHz (Genesys) to PCM16 16kHz (Gemini)
        pcm16_16k = pcmu_8k_to_pcm16_16k(pcmu_8k)

        self.logger.debug(f"Sending audio frame to Gemini: {len(pcm16_16k)} bytes (PCM16 16kHz)")

        # Encode as base64
        encoded = base64.b64encode(pcm16_16k).decode("utf-8")

        # Send as realtime_input
        msg = {
            "realtime_input": {
                "media_chunks": [
                    {
                        "mime_type": "audio/pcm;rate=16000",
                        "data": encoded
                    }
                ]
            }
        }

        await self._safe_send(json.dumps(msg))
        self._has_audio_in_buffer = True

    async def _send_client_content(self, text: Optional[str] = None, turn_complete: bool = True):
        """
        Send text content to Gemini.
        """
        parts = []
        if text:
            parts.append({"text": text})

        if parts:
            msg = {
                "client_content": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": parts
                        }
                    ],
                    "turn_complete": turn_complete
                }
            }
            await self._safe_send(json.dumps(msg))

    async def start_receiving(self, on_audio_callback):
        """
        Start receiving messages from Gemini Live API.
        """
        if not self.running or not self.ws or not is_websocket_open(self.ws):
            self.logger.warning(f"Cannot start receiving: running={self.running}, ws_exists={self.ws is not None}, ws_open={is_websocket_open(self.ws)}")
            return

        async def _read_loop():
            try:
                while self.running:
                    raw = await self.ws.recv()
                    try:
                        msg_dict = json.loads(raw)

                        if DEBUG == 'true':
                            msg_type = list(msg_dict.keys())[0] if msg_dict else 'unknown'
                            self.logger.debug(f"Received from Gemini: type={msg_type}")

                        # Handle different message types from Gemini
                        if "serverContent" in msg_dict:
                            await self._handle_server_content(msg_dict["serverContent"], on_audio_callback)
                        elif "toolCall" in msg_dict:
                            await self._handle_tool_call(msg_dict["toolCall"])
                        elif "toolCallCancellation" in msg_dict:
                            self.logger.info("[FunctionCall] Gemini cancelled tool calls")
                        elif "error" in msg_dict:
                            error = msg_dict["error"]
                            self.logger.error(f"[Gemini Error] {format_json(error)}")

                    except json.JSONDecodeError:
                        if DEBUG == 'true':
                            self.logger.debug("Received raw message from Gemini (non-JSON)")
            except websockets.exceptions.ConnectionClosed:
                self.logger.info("Gemini websocket closed.")
                self.running = False
            except Exception as e:
                self.logger.error(f"Error reading from Gemini: {e}")
                self.running = False

        self.read_task = asyncio.create_task(_read_loop())

    async def _handle_server_content(self, server_content: Dict[str, Any], on_audio_callback):
        """
        Handle serverContent messages from Gemini.
        """
        # Handle interruption
        if server_content.get("interrupted"):
            self.logger.info("[FunctionCall] Generation was interrupted (VAD detected user speech)")
            if self.on_speech_started_callback:
                await self.on_speech_started_callback()
            self._response_in_progress = False
            return

        # Handle turn complete
        if server_content.get("turnComplete"):
            self._turn_complete = True
            self._response_in_progress = False
            self.logger.info("[FunctionCall] Turn complete")

            # Check if we have a disconnect pending
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

        # Handle model turn (contains parts)
        if "modelTurn" in server_content:
            model_turn = server_content["modelTurn"]
            parts = model_turn.get("parts", [])

            for part in parts:
                # Handle audio data (inline_data with audio/pcm)
                if "inlineData" in part:
                    inline_data = part["inlineData"]
                    mime_type = inline_data.get("mimeType", "")

                    if "audio/pcm" in mime_type:
                        # Gemini outputs PCM16 24kHz - need to convert to PCMU 8kHz for Genesys
                        audio_b64 = inline_data.get("data", "")
                        if audio_b64:
                            pcm16_24k = base64.b64decode(audio_b64)
                            # Convert PCM16 24kHz to PCMU 8kHz
                            pcmu_8k = pcm16_24k_to_pcmu_8k(pcm16_24k)
                            on_audio_callback(pcmu_8k)

                # Handle text responses (for debugging/logging)
                if "text" in part:
                    text = part["text"]
                    self.logger.debug(f"[Gemini] Text response: {text[:100]}...")

                # Handle function calls
                if "functionCall" in part:
                    func_call = part["functionCall"]
                    func_name = func_call.get("name")
                    func_args = func_call.get("args", {})
                    func_id = func_call.get("id", f"call_{int(time.time() * 1000)}")

                    self.logger.info(f"[FunctionCall] Gemini function call: name={func_name}, id={func_id}")
                    await self._handle_function_call_execution(func_name, func_id, func_args)

        # Handle usage metadata
        if "usageMetadata" in server_content:
            usage = server_content["usageMetadata"]
            # Store last response for token metrics
            self.last_response = {"usage": self._convert_gemini_usage_to_openai_format(usage)}

    async def _handle_tool_call(self, tool_call: Dict[str, Any]):
        """
        Handle toolCall messages from Gemini.
        """
        function_calls = tool_call.get("functionCalls", [])
        for fc in function_calls:
            func_name = fc.get("name")
            func_id = fc.get("id", f"call_{int(time.time() * 1000)}")
            func_args = fc.get("args", {})

            self.logger.info(f"[FunctionCall] Gemini tool call: name={func_name}, id={func_id}")
            await self._handle_function_call_execution(func_name, func_id, func_args)

    async def _handle_function_call_execution(self, name: str, call_id: str, args: dict):
        """
        Execute function call (matches OpenAI behavior).
        """
        try:
            self.logger.info(f"[FunctionCall] Handling function call: name={name}, call_id={call_id}")

            if not name:
                self.logger.error(f"[FunctionCall] ERROR: Function name is empty or None for call_id={call_id}")
                await self._send_error_to_gemini(call_id, "Function name is missing")
                return

            if not call_id:
                self.logger.error(f"[FunctionCall] ERROR: call_id is empty or None for function={name}")
                return

            # Check if it's a Genesys data action tool
            if name in self.genesys_tool_handlers:
                await self._handle_genesys_tool_call(name, call_id, args or {})
                return

            # Handle call control functions
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
                closing_instruction = "Confirm the task is wrapped up and thank the caller in one short sentence."

            elif name in ("handoff_to_human", "end_conversation_with_escalation"):
                action = "end_conversation_with_escalation"
                reason = (args or {}).get("reason") or "Caller requested escalation"
                output_payload = {"result": "ok", "action": action, "reason": reason}
                info = reason
                self._disconnect_context = {"action": action, "reason": "transfer", "info": info}
                self._await_disconnect_on_done = True
                closing_instruction = "Let the caller know a live agent will take over and reassure them help is coming."

            else:
                self.logger.warning(f"[FunctionCall] Unknown function called: {name}. Sending error response.")
                output_payload = {"result": "error", "error": f"Unknown function: {name}"}

            # Send function response back to Gemini
            await self._send_function_response(call_id, output_payload)

            # If this is a call termination, request final farewell
            if closing_instruction:
                # Request Gemini to generate farewell audio
                msg = {
                    "client_content": {
                        "turns": [
                            {
                                "role": "user",
                                "parts": [{"text": closing_instruction}]
                            }
                        ],
                        "turn_complete": True
                    }
                }
                await self._safe_send(json.dumps(msg))

                if self._disconnect_context:
                    self.logger.info(
                        f"[FunctionCall] Scheduled Genesys disconnect after farewell: action={self._disconnect_context.get('action')}, reason={self._disconnect_context.get('reason')}, info={self._disconnect_context.get('info')}"
                    )

        except Exception as e:
            self.logger.error(f"[FunctionCall] ERROR: Exception handling function call {name}, call_id={call_id}: {e}", exc_info=True)
            await self._send_error_to_gemini(call_id, f"Internal error: {str(e)}")

    def register_genesys_tool_handlers(self, handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]]):
        """
        Register Genesys data action tool handlers.
        """
        self.genesys_tool_handlers = handlers or {}

    async def _handle_genesys_tool_call(self, name: str, call_id: str, args: Dict[str, Any]):
        """
        Handle Genesys data action tool calls.
        """
        handler = self.genesys_tool_handlers.get(name)
        if not handler:
            error_msg = f"No handler registered for tool {name}"
            self.logger.error(f"[FunctionCall] ERROR: {error_msg}")
            await self._send_error_to_gemini(call_id, error_msg)
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

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} failed with exception: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": type(exc).__name__,
                "message": error_msg
            }

        try:
            await self._send_function_response(call_id, output_payload)
        except Exception as send_exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send tool result to Gemini for call_id={call_id}: {send_exc}", exc_info=True)

    async def _send_function_response(self, call_id: str, payload: Dict[str, Any]):
        """
        Send function response back to Gemini.
        """
        try:
            if not call_id:
                self.logger.error(f"[FunctionCall] ERROR: Cannot send function response - call_id is empty")
                return

            if not isinstance(payload, dict):
                self.logger.error(f"[FunctionCall] ERROR: Payload must be a dictionary, got {type(payload).__name__}")
                return

            # Build Gemini function response format
            msg = {
                "tool_response": {
                    "function_responses": [
                        {
                            "id": call_id,
                            "name": payload.get("tool", "unknown"),
                            "response": payload
                        }
                    ]
                }
            }

            preview = json.dumps(payload)[:1024] if len(json.dumps(payload)) > 1024 else json.dumps(payload)
            self.logger.info(f"[FunctionCall] Sending function response to Gemini for call_id={call_id}. Response preview: {preview}")

            await self._safe_send(json.dumps(msg))
            self.logger.info(f"[FunctionCall] Successfully sent function response to Gemini for call_id={call_id}")

        except Exception as exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send function response for {call_id}: {exc}", exc_info=True)

    async def _send_error_to_gemini(self, call_id: str, error_message: str):
        """
        Send error response to Gemini.
        """
        try:
            if not call_id:
                self.logger.error(f"[FunctionCall] ERROR: Cannot send error - call_id is empty. Error was: {error_message}")
                return

            error_payload = {
                "status": "error",
                "message": error_message,
                "timestamp": time.time()
            }

            msg = {
                "tool_response": {
                    "function_responses": [
                        {
                            "id": call_id,
                            "response": error_payload
                        }
                    ]
                }
            }

            self.logger.info(f"[FunctionCall] Sending error to Gemini for call_id={call_id}: {error_message}")
            await self._safe_send(json.dumps(msg))

        except Exception as exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send error to Gemini for call_id={call_id}: {exc}", exc_info=True)

    def _convert_gemini_usage_to_openai_format(self, gemini_usage: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert Gemini usage metadata to OpenAI format for compatibility.
        """
        # Gemini provides: totalTokenCount, promptTokenCount, candidatesTokenCount
        # Map to OpenAI format for compatibility with existing code
        total_tokens = gemini_usage.get("totalTokenCount", 0)
        prompt_tokens = gemini_usage.get("promptTokenCount", 0)
        completion_tokens = gemini_usage.get("candidatesTokenCount", 0)

        return {
            "input_token_details": {
                "text_tokens": prompt_tokens,
                "audio_tokens": 0,  # Gemini doesn't separate audio/text tokens
                "cached_tokens_details": {
                    "text_tokens": 0,
                    "audio_tokens": 0
                }
            },
            "output_token_details": {
                "text_tokens": completion_tokens,
                "audio_tokens": 0
            }
        }

    async def close(self):
        """
        Close the Gemini WebSocket connection.
        """
        duration = time.time() - self.start_time
        self.logger.info(f"Closing Gemini connection after {duration:.2f}s")
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                self.logger.error(f"Error closing Gemini connection: {e}")
            self.ws = None
        if self.read_task:
            self.read_task.cancel()
            self.read_task = None

    async def await_summary(self, timeout: float = 10.0):
        """
        Generate and wait for conversation summary.
        """
        loop = asyncio.get_event_loop()
        self._summary_future = loop.create_future()
        try:
            # Request summary from Gemini
            summary_prompt = "Please provide a brief summary of this conversation."
            await self._send_client_content(text=summary_prompt, turn_complete=True)

            return await asyncio.wait_for(self._summary_future, timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.error("Timeout waiting for Gemini summary")
            return None
        finally:
            self._summary_future = None

    async def disconnect_session(self, reason="completed", info=""):
        """
        Disconnect the session.
        """
        await self.close()
