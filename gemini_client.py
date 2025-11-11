
import asyncio
import json
import time
import base64
import io
import copy
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from google import genai
    from google.genai import types
except ImportError as e:
    raise ImportError(
        "Google Generative AI SDK is not installed. "
        "Please install it with: pip install google-generativeai"
    ) from e

from config import (
    logger,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEBUG,
    GENESYS_RATE_WINDOW,
    GENESYS_PCMU_FRAME_SIZE,
    GENESYS_PCMU_SILENCE_BYTE
)
from utils import (
    format_json,
    create_final_system_prompt,
    pcmu_8k_to_pcm16_16k,
    pcm16_24k_to_pcmu_8k,
    resample_audio,
    decode_pcmu_to_pcm16
)


TERMINATION_GUIDANCE = """[CALL CONTROL]
Call `end_conversation_successfully` ONLY when BOTH of these conditions are met:
1. The caller's request has been completely addressed and resolved
2. The caller has explicitly confirmed they don't need any additional help or have no further questions

Call `end_conversation_with_escalation` when the caller explicitly requests a human, the task is blocked, or additional assistance is needed. Use the `reason` field to describe why escalation is required.

Before invoking any call-control function, you MUST ensure all required output session variables are properly filled with accurate information. After the function is called, deliver the appropriate farewell message as instructed."""


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


def _default_call_control_tools() -> List[Dict[str, Any]]:
    """Returns default call control function declarations for Gemini."""
    return [
        {
            "name": "end_conversation_successfully",
            "description": (
                "Gracefully end the phone call ONLY when the caller has BOTH: (1) had their request completely addressed, "
                "AND (2) explicitly confirmed they don't need any additional help or have no further questions. "
                "Provide a short summary of the completed task in the `summary` field. "
                "Do NOT call this function if the customer has not explicitly confirmed they are done."
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


def _build_function_declarations(tool_defs: List[Dict[str, Any]]) -> List[types.FunctionDeclaration]:
    """Convert generic tool definitions into Gemini FunctionDeclaration objects."""
    declarations: List[types.FunctionDeclaration] = []
    for tool in tool_defs:
        if not tool or "name" not in tool:
            continue
        cleaned_parameters = None
        parameters = tool.get("parameters")
        if isinstance(parameters, dict):
            cleaned_parameters = _clean_schema_for_gemini(parameters)
        schema = None
        if isinstance(cleaned_parameters, dict) and cleaned_parameters:
            try:
                schema = types.Schema(**cleaned_parameters)
            except Exception:
                schema = cleaned_parameters
        declarations.append(
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=schema
            )
        )
    return declarations


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
        self.escalation_prompt = None
        self.success_prompt = None
        self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS

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

        # Downlink audio buffering (Gemini PCM16 -> Genesys PCMU)
        self._on_audio_callback = None
        self._pending_pcmu_bytes = bytearray()
        self._pcmu_frame_size = GENESYS_PCMU_FRAME_SIZE

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
        if max_output_tokens is None:
            self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
        else:
            max_tokens_value = str(max_output_tokens).strip().lower()
            if max_tokens_value == "inf":
                self.max_output_tokens = None
            else:
                try:
                    parsed_tokens = int(max_output_tokens)
                    if parsed_tokens <= 0:
                        raise ValueError("max_output_tokens must be positive")
                    self.max_output_tokens = parsed_tokens
                except (TypeError, ValueError):
                    self.logger.warning(
                        f"Invalid max_output_tokens value '{max_output_tokens}'. "
                        f"Falling back to default ({DEFAULT_MAX_OUTPUT_TOKENS})."
                    )
                    self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS

        self.final_instructions = create_final_system_prompt(
            self.admin_instructions,
            language=language,
            customer_data=customer_data,
            agent_name=self.agent_name,
            company_name=self.company_name
        )

        # Map voice names from OpenAI to Gemini
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

        # Validate and map voice
        if voice and voice.strip():
            # Check if it's already a Gemini voice
            gemini_voices = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"]
            if voice in gemini_voices:
                self.voice = voice
            else:
                # Map from OpenAI voice names
                self.voice = voice_mapping.get(voice, "Kore")
        else:
            self.voice = "Kore"

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

        logged_function_names: List[str] = []

        try:
            self.logger.info(f"Connecting to Gemini Live API using model: {self.model}...")
            connect_start = time.time()

            # Initialize Gemini client with v1alpha API version
            self.client = genai.Client(
                api_key=self.api_key,
                http_options={'api_version': 'v1alpha'}
            )

            # Build function declarations for Gemini
            function_definition_dicts: List[Dict[str, Any]] = []

            # Add call control tools
            call_control_tools = _default_call_control_tools()
            function_definition_dicts.extend(call_control_tools)

            # Add custom tool definitions (Genesys data actions, etc.)
            if self.custom_tool_definitions:
                # Convert OpenAI tool format to Gemini format
                for tool in self.custom_tool_definitions:
                    if tool.get("type") == "function":
                        # Clean parameters to remove OpenAI-specific fields
                        parameters = tool.get("parameters", {})
                        cleaned_parameters = _clean_schema_for_gemini(parameters)

                        func_def = {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": cleaned_parameters
                        }
                        function_definition_dicts.append(func_def)

            # Wrap function declarations in tools structure (required by Gemini Live API)
            # Format: [{"function_declarations": [...]}]
            tools = None
            tool_config = None
            logged_function_names: List[str] = []
            if function_definition_dicts:
                logged_function_names = [tool.get("name", "unknown") for tool in function_definition_dicts]
                gemini_function_declarations = _build_function_declarations(function_definition_dicts)
                if gemini_function_declarations:
                    tools = [
                        types.Tool(
                            function_declarations=gemini_function_declarations
                        )
                    ]
                tool_config = self._build_tool_config(has_tools=bool(gemini_function_declarations))
            else:
                tool_config = None

            # Build configuration
            instructions_text = self.final_instructions
            extra_blocks = [TERMINATION_GUIDANCE]
            if self.tool_instruction_text:
                extra_blocks.append(self.tool_instruction_text)
            instructions_text = "\n\n".join([instructions_text] + extra_blocks) if extra_blocks else instructions_text

            # Build generation config with temperature
            generation_config = types.GenerateContentConfig(
                temperature=self.temperature,
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=self.voice
                        )
                    )
                ),
                max_output_tokens=self.max_output_tokens
            )

            config = types.LiveConnectConfig(
                generation_config=generation_config,
                system_instruction=instructions_text,
                tools=tools if tools else None,
                tool_config=tool_config
            )

            # Connect to Live API via async context manager to match SDK docs
            self.logger.info(f"Initiating Gemini Live API connection with model: {self.model}")
            session_cm = self.client.aio.live.connect(
                model=self.model,
                config=config
            )

            try:
                session = await session_cm.__aenter__()
            except Exception:
                exc_type, exc, tb = sys.exc_info()
                try:
                    await session_cm.__aexit__(exc_type, exc, tb)
                except Exception as exit_err:
                    self.logger.error(
                        f"Error while cleaning up failed Gemini session enter: {exit_err}",
                        exc_info=True
                    )
                raise

            # Only set state after successful context entry
            self.session = session
            self._session_context = session_cm

            connect_time = time.time() - connect_start
            self.logger.info(
                f"Gemini Live API connection established in {connect_time:.2f}s "
                f"(model={self.model}, voice={self.voice}, temperature={self.temperature})"
            )
            self.running = True

            if tools:
                fc_mode = None
                if tool_config and tool_config.function_calling_config:
                    fc_mode = tool_config.function_calling_config.mode.value
                mode_label = fc_mode or "AUTO"
                self.logger.info(
                    f"[FunctionCall] Configured {len(logged_function_names)} Gemini function declarations: {logged_function_names}; function_calling_mode={mode_label}"
                )
            else:
                self.logger.info("[FunctionCall] Gemini session started without custom tools")

            self.retry_count = 0

        except Exception as e:
            self.logger.error(f"Error establishing Gemini connection: {e}", exc_info=True)
            self.logger.error(f"Model: {self.model}, Voice: {self.voice}, Temperature: {self.temperature}")
            await self.close()
            raise RuntimeError(f"Failed to connect to Gemini Live API: {str(e)}")

    async def _safe_send(self, message: str):
        """
        Send a message to Gemini. This method exists for compatibility with OpenAI client interface,
        but most operations use SDK methods like session.send_client_content() instead.

        For Gemini, this handles OpenAI-format messages that need conversion.
        """
        async with self._lock:
            if not self.running or self.session is None:
                self.logger.warning("Cannot send message: session not running or not connected")
                return

            try:
                # Parse the message to check if it's OpenAI format
                import json
                msg_dict = json.loads(message)
                msg_type = msg_dict.get("type", "")

                if DEBUG == 'true':
                    self.logger.debug(f"_safe_send called with type={msg_type}")

                # For OpenAI "response.create" messages (used in summary generation),
                # we don't need to do anything as Gemini's await_summary() handles it differently
                if msg_type == "response.create":
                    self.logger.debug("Ignoring OpenAI response.create message - Gemini uses SDK methods")
                    return

                # For other message types, log a warning
                self.logger.warning(f"_safe_send called with unhandled message type: {msg_type}")

            except Exception as e:
                self.logger.error(f"Error in _safe_send: {e}")

    async def send_audio(self, pcmu_8k: bytes):
        """Send audio to Gemini (convert PCMU 8kHz to PCM16 16kHz)."""
        if not self.running or self.session is None:
            if DEBUG == 'true':
                self.logger.warning(f"Dropping audio frame: running={self.running}, session={self.session is not None}")
            return

        try:
            # Convert PCMU 8kHz (Genesys) to PCM16 16kHz (Gemini) using efficient audioop
            pcm16_16k = pcmu_8k_to_pcm16_16k(pcmu_8k)

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
        self._on_audio_callback = on_audio_callback
        self._pending_pcmu_bytes.clear()

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
                            # Gemini sends PCM16 24kHz, need to convert to PCMU 8kHz (Genesys)
                            try:
                                pcm16_24k = message.data
                                self.logger.debug(f"Received audio from Gemini: {len(pcm16_24k)} bytes (PCM16 24kHz)")

                                # Convert PCM16 24kHz to PCMU 8kHz using efficient audioop
                                pcmu_8k = pcm16_24k_to_pcmu_8k(pcm16_24k)
                                self._buffer_and_emit_pcmu(pcmu_8k)

                            except Exception as audio_err:
                                self.logger.error(f"Error processing audio from Gemini: {audio_err}", exc_info=True)

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
                                self._buffer_and_emit_pcmu(b"", force_flush=True)

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

                            if getattr(server_content, "interrupted", False):
                                if self._pending_pcmu_bytes:
                                    self.logger.info(
                                        f"Generation interrupted; dropping {len(self._pending_pcmu_bytes)} pending PCMU bytes"
                                    )
                                    self._pending_pcmu_bytes.clear()

                    except Exception as msg_err:
                        self.logger.error(f"Error processing Gemini message: {msg_err}", exc_info=True)

            except Exception as e:
                self.logger.error(f"Error in Gemini read loop: {e}", exc_info=True)
                self.running = False

        self.read_task = asyncio.create_task(_read_loop())

    async def _update_token_metrics(self, usage_metadata):
        """
        Update token tracking from Gemini usage metadata.

        For Gemini Live API:
        - usage_metadata.prompt_token_count = total input tokens for this turn
        - usage_metadata.candidates_token_count = total output tokens for this turn
        - Gemini Live API doesn't break down by modality, but for audio conversations,
          most tokens are audio tokens (input: 1 token/100ms, output: 1 token/50ms)
        """
        try:
            # Update totals (these are cumulative from Gemini)
            if hasattr(usage_metadata, 'total_token_count'):
                total = usage_metadata.total_token_count
            if hasattr(usage_metadata, 'prompt_token_count'):
                prompt_tokens_this_turn = usage_metadata.prompt_token_count
                self._total_prompt_tokens = prompt_tokens_this_turn

            if hasattr(usage_metadata, 'candidates_token_count'):
                candidates_tokens_this_turn = usage_metadata.candidates_token_count
                self._total_candidates_tokens = candidates_tokens_this_turn

            # For Live API audio conversations, tokens are primarily audio
            # Accumulate input audio tokens from this turn
            if prompt_tokens_this_turn > 0:
                self._token_details['input_audio_tokens'] += prompt_tokens_this_turn

            # Update detailed breakdown by modality (cumulative values from Gemini)
            if hasattr(usage_metadata, 'prompt_token_count_details'):
                details = usage_metadata.prompt_token_count_details
                if hasattr(details, 'audio_tokens'):
                    self._token_details['input_audio_tokens'] = details.audio_tokens
                if hasattr(details, 'text_tokens'):
                    self._token_details['input_text_tokens'] = details.text_tokens
                if hasattr(details, 'cached_content_token_count'):
                    self._token_details['input_cached_audio_tokens'] = details.cached_content_token_count

            if hasattr(usage_metadata, 'candidates_token_count_details'):
                details = usage_metadata.candidates_token_count_details
                if hasattr(details, 'audio_tokens'):
                    self._token_details['output_audio_tokens'] = details.audio_tokens
                if hasattr(details, 'text_tokens'):
                    self._token_details['output_text_tokens'] = details.text_tokens

            # Legacy fallback: If no detailed breakdown, estimate based on totals
            # For realtime audio conversations, most tokens are audio
            if self._token_details['input_audio_tokens'] == 0 and self._total_prompt_tokens > 0:
                self._token_details['input_audio_tokens'] = self._total_prompt_tokens

            if self._token_details['output_audio_tokens'] == 0 and self._total_candidates_tokens > 0:
                self._token_details['output_audio_tokens'] = self._total_candidates_tokens

            if DEBUG == 'true':
                self.logger.debug(
                    f"Token metrics updated: prompt={self._total_prompt_tokens}, "
                    f"candidates={self._total_candidates_tokens}, "
                    f"input_audio={self._token_details['input_audio_tokens']}, "
                    f"output_audio={self._token_details['output_audio_tokens']}"
                )

        except Exception as e:
            self.logger.error(f"Error updating token metrics: {e}", exc_info=True)

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
                self.logger.warning(f"[FunctionCall] Unknown function called: {name}")
                output_payload = {"result": "error", "error": f"Unknown function: {name}"}

            # Send function response back to Gemini
            function_response = types.FunctionResponse(
                id=call_id,
                name=name,
                response=output_payload
            )

            await self.session.send_tool_response(
                function_responses=[function_response]
            )

            self.logger.info(f"[FunctionCall] Sent function response for {name} (call_id={call_id})")

            if closing_instruction and self._disconnect_context:
                # Send the closing instruction to make Gemini say the farewell
                await self.session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=closing_instruction)]
                    ),
                    turn_complete=True
                )
                self.logger.info(
                    f"[FunctionCall] Sent closing instruction to Gemini. Scheduled disconnect after farewell: action={self._disconnect_context.get('action')}"
                )

        except Exception as e:
            self.logger.error(f"[FunctionCall] ERROR: Exception handling function call {name}: {e}", exc_info=True)

    def register_genesys_tool_handlers(self, handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]]):
        """Register handlers for Genesys data action tools."""
        self.genesys_tool_handlers = handlers or {}
        handler_count = len(self.genesys_tool_handlers)
        if handler_count:
            self.logger.info(f"[FunctionCall] Registered {handler_count} Genesys tool handler(s)")

    def _deliver_pcmu_frame(self, frame: bytes):
        callback = self._on_audio_callback
        if not callback:
            return
        try:
            callback(frame)
        except Exception as callback_err:
            self.logger.error(f"Error delivering audio frame to Genesys: {callback_err}", exc_info=True)

    def _buffer_and_emit_pcmu(self, pcmu_8k: bytes, force_flush: bool = False):
        """
        Accumulate Gemini PCM16→PCMU output and emit fixed-size frames required by Genesys.
        """
        if pcmu_8k:
            self._pending_pcmu_bytes.extend(pcmu_8k)

        if not self._on_audio_callback:
            return

        while len(self._pending_pcmu_bytes) >= self._pcmu_frame_size:
            frame = bytes(self._pending_pcmu_bytes[:self._pcmu_frame_size])
            del self._pending_pcmu_bytes[:self._pcmu_frame_size]
            self._deliver_pcmu_frame(frame)

        if force_flush and self._pending_pcmu_bytes:
            pad_len = (-len(self._pending_pcmu_bytes)) % self._pcmu_frame_size
            if pad_len:
                self._pending_pcmu_bytes.extend(
                    bytes([GENESYS_PCMU_SILENCE_BYTE]) * pad_len
                )
            while self._pending_pcmu_bytes:
                frame = bytes(self._pending_pcmu_bytes[:self._pcmu_frame_size])
                del self._pending_pcmu_bytes[:self._pcmu_frame_size]
                self._deliver_pcmu_frame(frame)

    def _build_tool_config(self, has_tools: bool) -> Optional[types.ToolConfig]:
        """
        Translate OpenAI-style tool_choice semantics into Gemini's function calling config.
        """
        if not has_tools:
            return None

        mode = types.FunctionCallingConfigMode.AUTO
        allowed_function_names = None

        choice = self.custom_tool_choice
        if isinstance(choice, str):
            normalized = choice.strip().lower()
            if normalized in ("none", "disabled"):
                mode = types.FunctionCallingConfigMode.NONE
            elif normalized in ("required", "force", "any"):
                mode = types.FunctionCallingConfigMode.ANY
            else:
                mode = types.FunctionCallingConfigMode.AUTO
        elif isinstance(choice, dict):
            if choice.get("type") == "function":
                func_name = (choice.get("function") or {}).get("name")
                if func_name:
                    allowed_function_names = [func_name]
                    mode = types.FunctionCallingConfigMode.VALIDATED

        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=mode,
                allowed_function_names=allowed_function_names
            )
        )

    async def _handle_genesys_tool_call(self, name: str, call_id: str, args: Dict[str, Any]):
        """Handle Genesys data action tool calls."""
        handler = self.genesys_tool_handlers.get(name)
        if not handler:
            error_msg = f"No handler registered for tool {name}"
            self.logger.error(f"[FunctionCall] ERROR: {error_msg}")
            return

        try:
            if not isinstance(args, dict):
                raise ValueError(f"Tool arguments must be a dictionary, got {type(args).__name__}")

            try:
                args_preview = json.dumps(args)[:512]
            except Exception:
                args_preview = str(args)[:512]
            self.logger.info(f"[FunctionCall] Calling handler for Genesys tool {name} with args: {args_preview}")

            result_payload = await handler(args)

            if result_payload is None:
                self.logger.warning(f"[FunctionCall] Tool {name} returned None")
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

            await self.session.send_tool_response(
                function_responses=[function_response]
            )

            self.logger.info(
                f"[FunctionCall] Sent Genesys tool response for {name} (call_id={call_id})"
            )
        except Exception as send_exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send tool result: {send_exc}", exc_info=True)

    async def close(self):
        """Close the Gemini session."""
        duration = time.time() - self.start_time
        self.logger.info(f"Closing Gemini connection after {duration:.2f}s")
        self.running = False
        self._buffer_and_emit_pcmu(b"", force_flush=True)
        self._pending_pcmu_bytes.clear()
        self._on_audio_callback = None

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
