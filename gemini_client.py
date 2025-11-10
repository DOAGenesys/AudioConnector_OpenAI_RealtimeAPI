
import asyncio
import json
import time
import base64
import io
from typing import Any, Awaitable, Callable, Dict, List, Optional

from google import genai
from google.genai import types

from config import (
    logger,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEBUG,
    GENESYS_RATE_WINDOW
)
from utils import format_json, create_final_system_prompt, decode_pcmu_to_pcm16, encode_pcm16_to_pcmu

try:
    import librosa
    import soundfile as sf
    AUDIO_LIBS_AVAILABLE = True
except ImportError:
    AUDIO_LIBS_AVAILABLE = False
    logger.warning("librosa or soundfile not available. Audio resampling will use basic conversion.")


TERMINATION_GUIDANCE = """[CALL CONTROL]
Call `end_conversation_successfully` when the caller's request has been resolved. Use the `summary` field to explain what was accomplished.
Call `end_conversation_with_escalation` when the caller explicitly requests a human, the task is blocked, or additional assistance is needed. Use the `reason` field to describe why escalation is required.
Always invoke the correct call-control tool as soon as the user's intent is clear. After confirming, deliver a short verbal acknowledgment."""


def _default_call_control_tools() -> List[Dict[str, Any]]:
    """Returns default call control function declarations for Gemini."""
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


class GeminiRealtimeClient:
    """
    Gemini Live API client that mirrors the OpenAIRealtimeClient interface
    for seamless integration with the AudioHook server.
    """

    def __init__(self, session_id: str, api_key: str, on_speech_started_callback=None):
        self.session_id = session_id
        self.api_key = api_key
        self.session = None
        self.running = False
        self.read_task = None
        self._lock = asyncio.Lock()
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

        # Token tracking for Gemini
        self._total_prompt_tokens = 0
        self._total_candidates_tokens = 0
        self._token_details = {
            'input_text_tokens': 0,
            'input_audio_tokens': 0,
            'input_cached_text_tokens': 0,
            'input_cached_audio_tokens': 0,
            'output_text_tokens': 0,
            'output_audio_tokens': 0
        }

        # Gemini client and session context
        self.client = None
        self.model = None
        self._session_context = None

    async def terminate_session(self, reason="completed", final_message=None):
        """Terminate the Gemini session."""
        try:
            if final_message and self.session:
                # Send a final message before closing
                await self.session.send_client_content(
                    turns=types.Content(
                        role="model",
                        parts=[types.Part(text=final_message)]
                    ),
                    turn_complete=True
                )

            await self.close()
        except Exception as e:
            self.logger.error(f"Error terminating session: {e}")
            raise

    async def handle_rate_limit(self):
        """Handle rate limiting with exponential backoff."""
        if self.retry_count >= 3:  # Max retries
            self.logger.error(f"[Rate Limit] Max retry attempts (3) reached.")
            return False

        self.retry_count += 1
        session_duration = time.time() - self.start_time
        self.logger.info(f"[Rate Limit] Current session duration: {session_duration:.2f}s")

        delay = GENESYS_RATE_WINDOW
        self.logger.warning(
            f"[Rate Limit] Hit rate limit, attempt {self.retry_count}/3. "
            f"Backing off for {delay}s."
        )

        self.running = False
        await asyncio.sleep(delay)
        self.running = True

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
        """Connect to Gemini Live API."""
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

        # Map voice names from OpenAI to Gemini (Gemini auto-selects voice based on language)
        # For now, we'll use the default voice, but this can be extended
        self.voice = voice if voice and voice.strip() else "Kore"

        try:
            self.temperature = float(temperature) if temperature else DEFAULT_TEMPERATURE
            # Gemini supports 0.0 to 2.0
            if not (0.0 <= self.temperature <= 2.0):
                logger.warning(f"Temperature {self.temperature} out of range [0.0, 2.0]. Using default: {DEFAULT_TEMPERATURE}")
                self.temperature = DEFAULT_TEMPERATURE
        except (TypeError, ValueError):
            logger.warning(f"Invalid temperature value: {temperature}. Using default: {DEFAULT_TEMPERATURE}")
            self.temperature = DEFAULT_TEMPERATURE

        # Use Gemini 2.5 Flash Native Audio model
        # Validate that the model is a Gemini model, not an OpenAI model
        default_gemini_model = "gemini-2.5-flash-native-audio-preview-09-2025"
        if model:
            # Check if the provided model is an OpenAI model (starts with "gpt-")
            if model.startswith("gpt-"):
                self.logger.warning(f"OpenAI model '{model}' specified but using Gemini. Using default Gemini model: {default_gemini_model}")
                self.model = default_gemini_model
            else:
                self.model = model
        else:
            self.model = default_gemini_model

        try:
            self.logger.info(f"Connecting to Gemini Live API using model: {self.model}...")
            connect_start = time.time()

            # Initialize Gemini client with v1alpha API version
            self.client = genai.Client(
                api_key=self.api_key,
                http_options={'api_version': 'v1alpha'}
            )

            # Build tool declarations for Gemini
            function_declarations = []

            # Add call control tools
            call_control_tools = _default_call_control_tools()
            function_declarations.extend(call_control_tools)

            # Add custom tool definitions (Genesys data actions, etc.)
            if self.custom_tool_definitions:
                # Convert OpenAI tool format to Gemini format
                for tool in self.custom_tool_definitions:
                    if tool.get("type") == "function":
                        func_def = {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {})
                        }
                        function_declarations.append(func_def)

            # Build configuration
            instructions_text = self.final_instructions
            extra_blocks = [TERMINATION_GUIDANCE]
            if self.tool_instruction_text:
                extra_blocks.append(self.tool_instruction_text)
            instructions_text = "\n\n".join([instructions_text] + extra_blocks) if extra_blocks else instructions_text

            # Wrap function declarations in the required tools format
            tools = None
            if function_declarations:
                tools = [types.Tool(function_declarations=function_declarations)]

            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=instructions_text,
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=self.voice
                        )
                    )
                ),
                tools=tools,
            )

            # Connect to Live API using async context manager
            # Create the context manager
            session_cm = self.client.aio.live.connect(
                model=self.model,
                config=config
            )

            # Enter the context manager manually to get the session
            self.session = await session_cm.__aenter__()

            # Only set _session_context after successful entry
            # (if __aenter__ fails, we shouldn't call __aexit__)
            self._session_context = session_cm

            connect_time = time.time() - connect_start
            self.logger.info(f"Gemini Live API connection established in {connect_time:.2f}s")
            self.running = True

            tool_names = [t.get("name", "unknown") for t in function_declarations]
            self.logger.info(
                f"[FunctionCall] Configured Gemini tools: {tool_names}; voice={self.voice}"
            )

            self.retry_count = 0

        except Exception as e:
            self.logger.error(f"Error establishing Gemini connection: {e}")
            await self.close()
            raise RuntimeError(f"Failed to connect to Gemini: {str(e)}")

    async def send_audio(self, pcmu_8k: bytes):
        """Send audio to Gemini (convert PCMU to PCM16 16kHz)."""
        if not self.running or self.session is None:
            if DEBUG == 'true':
                self.logger.warning(f"Dropping audio frame: running={self.running}, session={self.session is not None}")
            return

        try:
            # Convert PCMU 8kHz to PCM16
            pcm16_8k = decode_pcmu_to_pcm16(pcmu_8k)

            # Resample from 8kHz to 16kHz if audio libs available
            if AUDIO_LIBS_AVAILABLE:
                # Use librosa to resample
                import numpy as np
                audio_array = np.frombuffer(pcm16_8k, dtype=np.int16).astype(np.float32) / 32768.0
                audio_16k = librosa.resample(audio_array, orig_sr=8000, target_sr=16000)
                audio_16k_int16 = (audio_16k * 32768.0).astype(np.int16)
                pcm16_16k = audio_16k_int16.tobytes()
            else:
                # Simple duplication for 8kHz -> 16kHz (not ideal but works)
                pcm16_16k = pcm16_8k + pcm16_8k

            self.logger.debug(f"Sending audio to Gemini: {len(pcm16_16k)} bytes PCM16 16kHz")

            # Send as realtime input
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=pcm16_16k,
                    mime_type="audio/pcm;rate=16000"
                )
            )
            self._has_audio_in_buffer = True

        except Exception as e:
            self.logger.error(f"Error sending audio to Gemini: {e}")

    async def start_receiving(self, on_audio_callback):
        """Start receiving responses from Gemini."""
        if not self.running or not self.session:
            self.logger.warning(f"Cannot start receiving: running={self.running}, session={self.session is not None}")
            return

        async def _read_loop():
            try:
                async for message in self.session.receive():
                    if not self.running:
                        break

                    try:
                        if DEBUG == 'true':
                            self.logger.debug(f"Received from Gemini: {type(message).__name__}")

                        # Handle audio data
                        if message.data is not None:
                            # Gemini sends PCM16 24kHz, need to convert to PCMU 8kHz
                            try:
                                pcm16_24k = message.data

                                # Resample from 24kHz to 8kHz
                                if AUDIO_LIBS_AVAILABLE:
                                    import numpy as np
                                    audio_array = np.frombuffer(pcm16_24k, dtype=np.int16).astype(np.float32) / 32768.0
                                    audio_8k = librosa.resample(audio_array, orig_sr=24000, target_sr=8000)
                                    audio_8k_int16 = (audio_8k * 32768.0).astype(np.int16)
                                    pcm16_8k = audio_8k_int16.tobytes()
                                else:
                                    # Simple decimation (take every 3rd sample)
                                    samples = [pcm16_24k[i:i+2] for i in range(0, len(pcm16_24k), 6)]
                                    pcm16_8k = b''.join(samples)

                                # Convert to PCMU
                                pcmu_8k = encode_pcm16_to_pcmu(pcm16_8k)
                                on_audio_callback(pcmu_8k)

                            except Exception as audio_err:
                                self.logger.error(f"Error processing audio from Gemini: {audio_err}")

                        # Handle server content (turn completion, tool calls, etc.)
                        if message.server_content:
                            server_content = message.server_content

                            # Track tokens
                            if message.usage_metadata:
                                await self._update_token_metrics(message.usage_metadata)

                            # Handle turn complete
                            if server_content.turn_complete:
                                self._response_in_progress = False
                                self.logger.info("[FunctionCall] Turn complete from Gemini")

                                # Check if we need to disconnect
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
                                        self.logger.error(f"[FunctionCall] Exception invoking disconnect callback: {e}", exc_info=True)

                            # Handle model turn (contains function calls)
                            if server_content.model_turn:
                                model_turn = server_content.model_turn
                                self._response_in_progress = True

                                # Process parts for function calls
                                for part in model_turn.parts:
                                    if part.function_call:
                                        func_call = part.function_call
                                        name = func_call.name
                                        args = func_call.args if hasattr(func_call, 'args') else {}
                                        call_id = func_call.id if hasattr(func_call, 'id') else str(time.time())

                                        self.logger.info(f"[FunctionCall] Detected function call: name={name}, id={call_id}")
                                        await self._handle_function_call(name, call_id, args)

                            # Handle grounding metadata (if using Google Search)
                            if hasattr(server_content, 'grounding_metadata') and server_content.grounding_metadata:
                                self.logger.info(f"[Grounding] Received grounding metadata")

                    except Exception as msg_err:
                        self.logger.error(f"Error processing Gemini message: {msg_err}", exc_info=True)

            except Exception as e:
                self.logger.error(f"Error in Gemini read loop: {e}", exc_info=True)
                self.running = False

        self.read_task = asyncio.create_task(_read_loop())

    async def _update_token_metrics(self, usage_metadata):
        """Update token tracking from Gemini usage metadata."""
        try:
            # Update totals
            if hasattr(usage_metadata, 'total_token_count'):
                total = usage_metadata.total_token_count
            if hasattr(usage_metadata, 'prompt_token_count'):
                self._total_prompt_tokens = usage_metadata.prompt_token_count
            if hasattr(usage_metadata, 'candidates_token_count'):
                self._total_candidates_tokens = usage_metadata.candidates_token_count

            # Update detailed breakdown by modality
            if hasattr(usage_metadata, 'response_tokens_details'):
                for detail in usage_metadata.response_tokens_details:
                    if isinstance(detail, types.ModalityTokenCount):
                        modality = detail.modality
                        count = detail.token_count

                        # Map modality to our tracking
                        if modality == "TEXT":
                            self._token_details['output_text_tokens'] += count
                        elif modality == "AUDIO":
                            self._token_details['output_audio_tokens'] += count

            # For input tokens, we'll estimate based on prompt tokens
            # Gemini doesn't break down input by modality in the same way
            # We'll assume audio dominates for realtime voice
            self._token_details['input_audio_tokens'] = self._total_prompt_tokens

            if DEBUG == 'true':
                self.logger.debug(f"Token metrics updated: prompt={self._total_prompt_tokens}, candidates={self._total_candidates_tokens}")

        except Exception as e:
            self.logger.error(f"Error updating token metrics: {e}")

    async def _handle_function_call(self, name: str, call_id: str, args: dict):
        """Handle function calls from Gemini."""
        try:
            self.logger.info(f"[FunctionCall] Handling function call: name={name}, call_id={call_id}")

            if not name:
                self.logger.error(f"[FunctionCall] ERROR: Function name is empty for call_id={call_id}")
                return

            # Check if this is a Genesys tool
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
                summary = (args or {}).get("summary") or "Customer confirmed the request was completed."
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
                self.logger.warning(f"[FunctionCall] Unknown function called: {name}")
                output_payload = {"result": "error", "error": f"Unknown function: {name}"}

            # Send function response back to Gemini
            function_response = types.FunctionResponse(
                id=call_id,
                name=name,
                response=output_payload
            )

            await self.session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(function_response=function_response)]
                ),
                turn_complete=True
            )

            self.logger.info(f"[FunctionCall] Sent function response for {name} (call_id={call_id})")

            if closing_instruction and self._disconnect_context:
                self.logger.info(
                    f"[FunctionCall] Scheduled disconnect after farewell: action={self._disconnect_context.get('action')}"
                )

        except Exception as e:
            self.logger.error(f"[FunctionCall] ERROR: Exception handling function call {name}: {e}", exc_info=True)

    def register_genesys_tool_handlers(self, handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]]):
        """Register handlers for Genesys data action tools."""
        self.genesys_tool_handlers = handlers or {}

    async def _handle_genesys_tool_call(self, name: str, call_id: str, args: Dict[str, Any]):
        """Handle Genesys data action tool calls."""
        handler = self.genesys_tool_handlers.get(name)
        if not handler:
            error_msg = f"No handler registered for tool {name}"
            self.logger.error(f"[FunctionCall] ERROR: {error_msg}")
            return

        try:
            self.logger.info(f"[FunctionCall] Calling handler for Genesys tool {name}")
            result_payload = await handler(args)

            if result_payload is None:
                self.logger.warning(f"[FunctionCall] Tool {name} returned None")
                result_payload = {}

            output_payload = {
                "status": "ok",
                "tool": name,
                "result": result_payload
            }

            self.logger.info(f"[FunctionCall] Genesys tool {name} executed successfully")

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {str(exc)}"
            self.logger.error(f"[FunctionCall] ERROR: Tool {name} failed: {exc}", exc_info=True)
            output_payload = {
                "status": "error",
                "tool": name,
                "error_type": type(exc).__name__,
                "message": error_msg
            }

        try:
            # Send function response back to Gemini
            function_response = types.FunctionResponse(
                id=call_id,
                name=name,
                response=output_payload
            )

            await self.session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(function_response=function_response)]
                ),
                turn_complete=True
            )

            self.logger.info(f"[FunctionCall] Sent Genesys tool response for {name} (call_id={call_id})")
        except Exception as send_exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send tool result: {send_exc}", exc_info=True)

    async def close(self):
        """Close the Gemini session."""
        duration = time.time() - self.start_time
        self.logger.info(f"Closing Gemini connection after {duration:.2f}s")
        self.running = False

        # Exit the async context manager if it exists
        if self._session_context:
            try:
                await self._session_context.__aexit__(None, None, None)
                self.logger.debug("Successfully exited Gemini session context")
            except Exception as e:
                self.logger.error(f"Error exiting Gemini session context: {e}")
            self._session_context = None

        if self.session:
            self.session = None

        if self.read_task:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass
            self.read_task = None

    async def await_summary(self, timeout: float = 10.0):
        """Generate a summary of the conversation."""
        # For Gemini, we can request a summary by sending a specific prompt
        loop = asyncio.get_event_loop()
        self._summary_future = loop.create_future()

        try:
            # Send summary request
            await self.session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text="""
Please analyze this conversation and provide a structured summary including:
{
    "main_topics": [],
    "key_decisions": [],
    "action_items": [],
    "sentiment": ""
}
""")]
                ),
                turn_complete=True
            )

            return await asyncio.wait_for(self._summary_future, timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.error("Timeout generating summary")
            return None
        finally:
            self._summary_future = None

    async def disconnect_session(self, reason="completed", info=""):
        """Disconnect the session."""
        await self.close()

    def get_token_metrics(self) -> Dict[str, str]:
        """
        Get token usage metrics in a format compatible with output variables.
        Returns dict with string values for all token counts.
        """
        return {
            "TOTAL_INPUT_TEXT_TOKENS": str(self._token_details.get('input_text_tokens', 0)),
            "TOTAL_INPUT_CACHED_TEXT_TOKENS": str(self._token_details.get('input_cached_text_tokens', 0)),
            "TOTAL_INPUT_AUDIO_TOKENS": str(self._token_details.get('input_audio_tokens', 0)),
            "TOTAL_INPUT_CACHED_AUDIO_TOKENS": str(self._token_details.get('input_cached_audio_tokens', 0)),
            "TOTAL_OUTPUT_TEXT_TOKENS": str(self._token_details.get('output_text_tokens', 0)),
            "TOTAL_OUTPUT_AUDIO_TOKENS": str(self._token_details.get('output_audio_tokens', 0))
        }
