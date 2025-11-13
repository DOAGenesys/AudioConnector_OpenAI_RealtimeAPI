"""
Gemini Live API Client - Refactored from scratch for production use.

This client implements the Gemini Live API following official documentation:
https://ai.google.dev/gemini-api/docs/live
https://ai.google.dev/gemini-api/docs/live-tools

Key features:
- Native audio support (24kHz output, 16kHz input)
- Function calling with Genesys data actions
- Call control functions (end conversation, escalation)
- Voice Activity Detection (VAD)
- Token tracking and session management
"""

import asyncio
import json
import time
import base64
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
    GENESYS_PCMU_SILENCE_BYTE,
)
from utils import (
    format_json,
    create_final_system_prompt,
    pcm16_24k_to_pcmu_8k,
    resample_audio,
    decode_pcmu_to_pcm16
)


# VAD and silence detection constants
PCM16_SILENCE_FLOOR = 750
VAD_SILENCE_THRESHOLD_FRAMES = 50  # ~1 second at 20ms frames
VAD_IDLE_CHECK_INTERVAL = 0.2

# Call control guidance integrated into system prompt
CALL_CONTROL_GUIDANCE = """
## Call Control Instructions

You have two special functions to end the conversation:

1. **end_conversation_successfully** - Use this ONLY when:
   - The customer's request has been COMPLETELY fulfilled
   - AND the customer explicitly confirms they don't need anything else
   - Provide a brief summary of what was accomplished

2. **end_conversation_with_escalation** - Use this when:
   - The customer explicitly requests to speak with a human
   - You cannot complete their request due to system limitations
   - The situation requires human intervention
   - Provide a clear reason for the escalation

IMPORTANT: Do NOT end the conversation prematurely. Always confirm the customer is satisfied before calling end_conversation_successfully.
"""


class GeminiRealtimeClient:
    """
    Gemini Live API client compatible with AudioHook server interface.

    Implements bidirectional audio streaming, function calling, and VAD
    following Gemini Live API best practices.
    """

    def __init__(self, session_id: str, api_key: str, on_speech_started_callback=None):
        """Initialize Gemini client with session context."""
        self.session_id = session_id
        self.api_key = api_key
        self.session = None
        self._session_context_manager = None
        self.running = False
        self.read_task = None
        self._lock = asyncio.Lock()
        self.logger = logger.getChild(f"GeminiClient_{session_id}")
        self.start_time = time.time()

        # Configuration
        self.voice = "Kore"  # Default Gemini voice
        self.model = "gemini-2.5-flash-native-audio-preview-09-2025"
        self.temperature = DEFAULT_TEMPERATURE
        self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
        self.admin_instructions = None
        self.final_instructions = None
        self.agent_name = None
        self.company_name = None

        # Callbacks
        self.on_speech_started_callback = on_speech_started_callback
        self.on_end_call_request = None
        self.on_handoff_request = None
        self.escalation_prompt = None
        self.success_prompt = None

        # Tool/function calling
        self.custom_tool_definitions: List[Dict[str, Any]] = []
        self.tool_instruction_text: Optional[str] = None
        self.genesys_tool_handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}

        # State tracking
        self._response_in_progress = False
        self._has_audio_in_buffer = False
        self._await_disconnect_on_done = False
        self._disconnect_context = None
        self._summary_future = None

        # Audio streaming (Genesys PCMU 8kHz -> Gemini PCM16 16kHz)
        self._audio_stream_open = False
        self._consecutive_silence_frames = 0
        self._last_audio_time = 0.0
        self._vad_monitor_task = None

        # Audio output buffering (Gemini PCM16 24kHz -> Genesys PCMU 8kHz)
        self._on_audio_callback = None
        self._pending_pcmu_bytes = bytearray()
        self._pcmu_frame_size = GENESYS_PCMU_FRAME_SIZE

        # Token tracking (Gemini format)
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._token_details = {
            'input_text': 0,
            'input_audio': 0,
            'input_cached': 0,
            'output_text': 0,
            'output_audio': 0,
        }

        # Retry/rate limiting
        self.retry_count = 0
        self.last_retry_time = 0

        # Customer context for personalization
        self.language = None
        self.customer_data = None

        self.logger.info(f"Gemini client initialized for session {session_id}")

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
        tool_choice: Optional[Any] = None  # Not used by Gemini, kept for interface compatibility
    ):
        """
        Connect to Gemini Live API and configure the session.

        Args:
            instructions: System instructions for the AI
            voice: Voice name (will be mapped to Gemini voice)
            temperature: Sampling temperature (0.0-2.0 for Gemini)
            model: Model name (defaults to Gemini 2.5 Flash Native Audio)
            max_output_tokens: Maximum output tokens
            agent_name: Name of the AI agent
            company_name: Company name for personalization
            tool_definitions: List of function definitions (OpenAI format)
            tool_instructions: Additional instructions about tools
            tool_choice: Tool choice policy (for interface compatibility)
        """
        self.logger.info("[Gemini] Starting connection...")

        # Store configuration
        self.admin_instructions = instructions
        self.agent_name = agent_name
        self.company_name = company_name
        self.custom_tool_definitions = tool_definitions or []
        self.tool_instruction_text = tool_instructions

        # Build final system prompt
        self.final_instructions = create_final_system_prompt(
            self.admin_instructions,
            language=self.language,
            customer_data=self.customer_data,
            agent_name=self.agent_name,
            company_name=self.company_name
        )

        # Add call control guidance and tool instructions
        instruction_blocks = [self.final_instructions, CALL_CONTROL_GUIDANCE]
        if self.tool_instruction_text:
            instruction_blocks.append(self.tool_instruction_text)
        system_instruction = "\n\n".join(instruction_blocks)

        # Map voice names (OpenAI -> Gemini)
        voice_map = {
            "alloy": "Puck",
            "ash": "Charon",
            "ballad": "Aoede",
            "coral": "Kore",
            "echo": "Puck",
            "sage": "Fenrir",
            "shimmer": "Aoede",
            "verse": "Charon"
        }

        gemini_voices = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"]
        if voice:
            if voice in gemini_voices:
                self.voice = voice
            else:
                self.voice = voice_map.get(voice, "Kore")

        # Validate temperature (Gemini supports 0.0-2.0)
        if temperature is not None:
            try:
                temp = float(temperature)
                self.temperature = max(0.0, min(2.0, temp))
                if temp != self.temperature:
                    self.logger.warning(f"Temperature {temp} clamped to {self.temperature}")
            except (TypeError, ValueError):
                self.logger.warning(f"Invalid temperature {temperature}, using {DEFAULT_TEMPERATURE}")
                self.temperature = DEFAULT_TEMPERATURE

        # Validate model (ensure it's a Gemini model)
        if model and not model.startswith("gpt-"):  # Not an OpenAI model
            self.model = model
        else:
            if model and model.startswith("gpt-"):
                self.logger.warning(f"OpenAI model {model} specified, using Gemini default")
            self.model = "gemini-2.5-flash-native-audio-preview-09-2025"

        # Validate max_output_tokens
        if max_output_tokens:
            if str(max_output_tokens).lower() == "inf":
                self.max_output_tokens = None
            else:
                try:
                    tokens = int(max_output_tokens)
                    self.max_output_tokens = tokens if tokens > 0 else None
                except (TypeError, ValueError):
                    self.logger.warning(f"Invalid max_output_tokens {max_output_tokens}, using default")
                    self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS

        # Build function declarations for Gemini
        # Following Gemini docs: tools = [{"function_declarations": [...]}]
        function_declarations = self._build_function_declarations()

        # Log configuration
        func_names = [f["name"] for f in function_declarations] if function_declarations else []
        self.logger.info(
            f"[FunctionCall] Connecting with {len(func_names)} functions: {func_names}"
        )
        self.logger.info(f"[FunctionCall] Model: {self.model}, Voice: {self.voice}, Temp: {self.temperature}")

        try:
            # Initialize Gemini client with v1alpha for latest features
            self.client = genai.Client(
                api_key=self.api_key,
                http_options={'api_version': 'v1alpha'}
            )

            # Build configuration following official docs pattern
            config = self._build_config(system_instruction, function_declarations)

            # Log debug info
            if DEBUG == 'true':
                try:
                    config_dict = config.model_dump(exclude_none=True)
                    self.logger.debug(f"[FunctionCall] Config: {format_json(config_dict)}")
                except:
                    pass

            # Connect using async context manager (as per SDK docs)
            self.logger.info(f"Connecting to Gemini Live API: {self.model}")
            connect_start = time.time()

            session_cm = self.client.aio.live.connect(
                model=self.model,
                config=config
            )

            # Enter the context manager
            self.session = await session_cm.__aenter__()
            self._session_context_manager = session_cm

            connect_time = time.time() - connect_start
            self.logger.info(
                f"Gemini Live API connected in {connect_time:.2f}s "
                f"(model={self.model}, voice={self.voice})"
            )

            # Mark as running and start VAD monitor
            self.running = True
            self._last_audio_time = time.monotonic()
            self._start_vad_monitor()

            self.retry_count = 0

        except Exception as e:
            self.logger.error(f"Failed to connect to Gemini: {e}", exc_info=True)
            await self.close()
            raise RuntimeError(f"Gemini connection failed: {e}")

    def _build_function_declarations(self) -> List[Dict[str, Any]]:
        """
        Build function declarations in Gemini format.

        Gemini expects:
        {
            "name": "function_name",
            "description": "...",
            "parameters": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }

        Note: Gemini does NOT support "strict" or "additionalProperties"
        """
        declarations = []

        # Add call control functions
        declarations.extend([
            {
                "name": "end_conversation_successfully",
                "description": (
                    "End the phone call successfully when the customer's request is completely fulfilled "
                    "AND the customer explicitly confirms they don't need anything else. "
                    "Provide a summary of what was accomplished."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Brief summary of what was accomplished in the conversation"
                        }
                    },
                    "required": ["summary"]
                }
            },
            {
                "name": "end_conversation_with_escalation",
                "description": (
                    "End the phone call and transfer to a human agent when the customer requests it "
                    "or the task cannot be completed by AI. Provide a clear reason for escalation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Clear explanation of why human assistance is needed"
                        }
                    },
                    "required": ["reason"]
                }
            }
        ])

        # Add custom tools (Genesys data actions, etc.)
        if self.custom_tool_definitions:
            for tool in self.custom_tool_definitions:
                if tool.get("type") != "function":
                    continue

                # Extract and clean parameters
                params = tool.get("parameters", {})
                cleaned_params = self._clean_parameters_for_gemini(params)

                func_decl = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": cleaned_params
                }
                declarations.append(func_decl)

        return declarations

    def _clean_parameters_for_gemini(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Remove OpenAI-specific fields from parameter schema.

        Gemini does NOT support:
        - "strict": OpenAI structured outputs
        - "additionalProperties": Can cause schema validation issues
        """
        if not isinstance(params, dict):
            return params

        # Deep copy to avoid modifying original
        import copy
        cleaned = copy.deepcopy(params)

        # Remove unsupported fields
        cleaned.pop("strict", None)
        cleaned.pop("additionalProperties", None)

        # Recursively clean nested objects
        if "properties" in cleaned and isinstance(cleaned["properties"], dict):
            for key, value in cleaned["properties"].items():
                if isinstance(value, dict):
                    cleaned["properties"][key] = self._clean_parameters_for_gemini(value)

        if "items" in cleaned and isinstance(cleaned["items"], dict):
            cleaned["items"] = self._clean_parameters_for_gemini(cleaned["items"])

        return cleaned

    def _build_config(
        self,
        system_instruction: str,
        function_declarations: List[Dict[str, Any]]
    ) -> types.LiveConnectConfig:
        """
        Build Gemini Live API configuration.

        Following official docs pattern:
        https://ai.google.dev/gemini-api/docs/live

        CRITICAL: Includes Google Search grounding workaround for function calling.
        Per GitHub issue reports, adding google_search tool enables function calling to work properly.
        """
        # Speech configuration (voice)
        speech_config = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=self.voice
                )
            )
        )

        # VAD configuration - using automatic mode as per Gemini docs
        realtime_input_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,  # Use Gemini's automatic VAD
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=20,
                silence_duration_ms=900  # ~1 second as per docs
            )
        )

        # Build config
        config_dict = {
            "response_modalities": [types.Modality.AUDIO],
            "speech_config": speech_config,
            "system_instruction": system_instruction,
            "temperature": self.temperature,
            "realtime_input_config": realtime_input_config,
            # Enable transcriptions for debugging
            "input_audio_transcription": types.AudioTranscriptionConfig(),
            "output_audio_transcription": types.AudioTranscriptionConfig(),
        }

        if self.max_output_tokens is not None:
            config_dict["max_output_tokens"] = self.max_output_tokens

        # Add tools if we have function declarations
        # Following Gemini docs: tools = [{"function_declarations": [...]}, {"google_search": {}}]
        if function_declarations:
            # Convert dict declarations to typed FunctionDeclaration objects
            typed_declarations = []
            for decl in function_declarations:
                typed_decl = types.FunctionDeclaration(
                    name=decl["name"],
                    description=decl.get("description", ""),
                    parameters=decl.get("parameters")
                )
                typed_declarations.append(typed_decl)

            # CRITICAL FIX: Add Google Search grounding alongside function declarations
            # Per GitHub issue: "Tool calling works for me too, when set with grounding..."
            # This enables the model to properly recognize and execute function calls
            tools_list = [
                types.Tool(function_declarations=typed_declarations),
                types.Tool(google_search=types.GoogleSearch())  # Grounding workaround
            ]

            config_dict["tools"] = tools_list

            self.logger.info(
                f"[FunctionCall] Configured {len(typed_declarations)} function declarations with Google Search grounding"
            )

        return types.LiveConnectConfig(**config_dict)

    async def send_audio(self, pcmu_8k: bytes):
        """
        Send audio from Genesys (PCMU 8kHz) to Gemini (PCM16 16kHz).

        Following Gemini docs pattern:
        session.sendRealtimeInput({ audio: { data: base64Audio, mimeType: "audio/pcm;rate=16000" } })
        """
        if not self.running or self.session is None:
            return

        try:
            # Convert PCMU 8kHz to PCM16 8kHz
            pcm16_8k = decode_pcmu_to_pcm16(pcmu_8k)

            # Update activity timestamp
            self._last_audio_time = time.monotonic()

            # Check if silence
            is_silence = self._is_silence(pcm16_8k)

            if is_silence:
                self._consecutive_silence_frames += 1
                # Flush stream after ~1 second of silence
                if self._consecutive_silence_frames >= VAD_SILENCE_THRESHOLD_FRAMES:
                    await self._flush_audio_stream()
                    return  # Don't send silence frames after flush
            else:
                self._consecutive_silence_frames = 0
                # Open audio stream if closed
                if not self._audio_stream_open:
                    self._audio_stream_open = True

            # Resample to 16kHz (Gemini's expected input rate)
            pcm16_16k = resample_audio(pcm16_8k, 8000, 16000, 2)

            # Send to Gemini as realtime input
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=pcm16_16k,
                    mime_type="audio/pcm;rate=16000"
                )
            )

            self._has_audio_in_buffer = True

        except Exception as e:
            self.logger.error(f"Error sending audio to Gemini: {e}", exc_info=True)

    def _is_silence(self, pcm16_data: bytes) -> bool:
        """Check if PCM16 audio frame is silence."""
        if not pcm16_data:
            return True

        try:
            # Convert bytes to int16 samples
            import array
            samples = array.array('h', pcm16_data)
            if not samples:
                return True

            # Check if all samples below threshold
            max_amplitude = max(abs(s) for s in samples)
            return max_amplitude < PCM16_SILENCE_FLOOR
        except:
            return False

    async def _flush_audio_stream(self):
        """Flush audio stream when user stops speaking (as per Gemini docs)."""
        if not self._audio_stream_open or not self.session:
            return

        try:
            # Send audio_stream_end signal (as per Gemini docs)
            await self.session.send_realtime_input(audio_stream_end=True)
            self.logger.debug("[Gemini] Sent audio_stream_end")

            self._audio_stream_open = False
            self._consecutive_silence_frames = 0

        except Exception as e:
            self.logger.error(f"Error flushing audio stream: {e}", exc_info=True)

    async def start_receiving(self, on_audio_callback):
        """
        Start receiving responses from Gemini.

        Handles:
        - Audio data (PCM16 24kHz -> PCMU 8kHz)
        - Server content (turn completion, tool calls)
        - Function calls (via tool_call path - this is the primary path per GitHub reports)
        - Token usage tracking
        """
        if not self.running or not self.session:
            self.logger.warning("Cannot start receiving: session not ready")
            return

        self._on_audio_callback = on_audio_callback
        self._pending_pcmu_bytes.clear()

        async def _read_loop():
            try:
                async for message in self.session.receive():
                    if not self.running:
                        break

                    try:
                        # Process audio data
                        if message.data is not None:
                            self._process_audio_output(message.data)

                        # CRITICAL: Process tool calls FIRST (this is the primary path per GitHub)
                        # Per GitHub comment: "message.ToolCall.FunctionCalls always works"
                        if message.tool_call:
                            self.logger.debug("[FunctionCall] Detected tool_call message path")
                            await self._process_tool_call(message.tool_call)

                        # Process server content (turn completion, etc.)
                        # This also checks for function calls in model_turn as a fallback
                        if message.server_content:
                            await self._process_server_content(message.server_content)

                        # Track token usage
                        if message.usage_metadata:
                            self._update_token_tracking(message.usage_metadata)

                    except Exception as msg_err:
                        self.logger.error(f"Error processing message: {msg_err}", exc_info=True)

            except Exception as e:
                self.logger.error(f"Error in receive loop: {e}", exc_info=True)
                self.running = False

        self.read_task = asyncio.create_task(_read_loop())

    def _process_audio_output(self, pcm16_24k: bytes):
        """
        Process audio from Gemini (PCM16 24kHz) and convert to Genesys format (PCMU 8kHz).

        Gemini outputs PCM16 at 24kHz, but Genesys expects PCMU at 8kHz.
        """
        try:
            self.logger.debug(f"Received audio from Gemini: {len(pcm16_24k)} bytes (PCM16 24kHz)")

            # Convert PCM16 24kHz to PCMU 8kHz
            pcmu_8k = pcm16_24k_to_pcmu_8k(pcm16_24k)

            # Buffer and emit in proper frame sizes
            self._buffer_and_send_pcmu(pcmu_8k)

        except Exception as e:
            self.logger.error(f"Error processing audio output: {e}", exc_info=True)

    def _buffer_and_send_pcmu(self, pcmu_data: bytes, flush: bool = False):
        """Buffer PCMU data and send in correct frame sizes to Genesys."""
        if pcmu_data:
            self._pending_pcmu_bytes.extend(pcmu_data)

        # Send complete frames
        while len(self._pending_pcmu_bytes) >= self._pcmu_frame_size:
            frame = bytes(self._pending_pcmu_bytes[:self._pcmu_frame_size])
            del self._pending_pcmu_bytes[:self._pcmu_frame_size]
            self._send_pcmu_frame(frame)

        # Flush remaining data with padding if needed
        if flush and self._pending_pcmu_bytes:
            # Pad to frame size
            pad_len = self._pcmu_frame_size - len(self._pending_pcmu_bytes)
            self._pending_pcmu_bytes.extend(bytes([GENESYS_PCMU_SILENCE_BYTE]) * pad_len)

            while self._pending_pcmu_bytes:
                frame = bytes(self._pending_pcmu_bytes[:self._pcmu_frame_size])
                del self._pending_pcmu_bytes[:self._pcmu_frame_size]
                self._send_pcmu_frame(frame)

    def _send_pcmu_frame(self, frame: bytes):
        """Send a PCMU frame to Genesys via callback."""
        if self._on_audio_callback:
            try:
                self._on_audio_callback(frame)
            except Exception as e:
                self.logger.error(f"Error in audio callback: {e}", exc_info=True)

    async def _process_server_content(self, server_content):
        """
        Process server content from Gemini.

        Handles:
        - Turn completion
        - Model turns (containing function calls via model_turn.parts - fallback path)
        - Interruptions
        - Transcriptions
        """
        try:
            # Log transcriptions
            if hasattr(server_content, 'input_transcription'):
                transcript = server_content.input_transcription
                if transcript and hasattr(transcript, 'text') and transcript.text:
                    self.logger.info(f"[Gemini] Input transcription: '{transcript.text}'")

            if hasattr(server_content, 'output_transcription'):
                transcript = server_content.output_transcription
                if transcript and hasattr(transcript, 'text') and transcript.text:
                    self.logger.debug(f"[Gemini] Output transcription: '{transcript.text}'")

            # Handle turn complete
            if server_content.turn_complete:
                self._response_in_progress = False
                self.logger.info("[FunctionCall] Turn complete from Gemini")

                # Flush any remaining audio
                self._buffer_and_send_pcmu(b"", flush=True)

                # Check if we need to disconnect after this turn
                if self._await_disconnect_on_done and self._disconnect_context:
                    await self._handle_disconnect_callback()

            # Handle model turn (contains function calls via parts - this is a fallback path)
            # Per GitHub: The primary path is message.tool_call, but we check here as fallback
            if hasattr(server_content, 'model_turn') and server_content.model_turn:
                self._response_in_progress = True
                model_turn = server_content.model_turn

                if hasattr(model_turn, 'parts') and model_turn.parts:
                    has_function_calls = False
                    for part in model_turn.parts:
                        # Check for function call in part
                        if hasattr(part, 'function_call') and part.function_call:
                            if not has_function_calls:
                                self.logger.info("[FunctionCall] Detected function call in model_turn.parts (fallback path)")
                                has_function_calls = True
                            await self._handle_function_call_from_part(part.function_call)

            # Handle interruptions
            if hasattr(server_content, 'interrupted') and server_content.interrupted:
                self.logger.info("[Gemini] Generation interrupted")
                self._pending_pcmu_bytes.clear()

        except Exception as e:
            self.logger.error(f"Error processing server content: {e}", exc_info=True)

    async def _process_tool_call(self, tool_call):
        """
        Process tool call message (PRIMARY path for function calls per GitHub reports).

        Per GitHub comment: "message.ToolCall.FunctionCalls always works and contains what we actually expect"
        """
        try:
            if not hasattr(tool_call, 'function_calls'):
                self.logger.warning("[FunctionCall] tool_call message has no function_calls attribute")
                return

            function_calls = tool_call.function_calls or []
            if not function_calls:
                self.logger.warning("[FunctionCall] tool_call.function_calls is empty")
                return

            self.logger.info(f"[FunctionCall] ✓ Received {len(function_calls)} function call(s) via tool_call path")

            for idx, func_call in enumerate(function_calls):
                func_name = getattr(func_call, 'name', 'unknown')
                self.logger.info(f"[FunctionCall] Processing call {idx+1}/{len(function_calls)}: {func_name}")
                await self._handle_function_call_from_part(func_call)

        except Exception as e:
            self.logger.error(f"[FunctionCall] Error processing tool call: {e}", exc_info=True)

    async def _handle_function_call_from_part(self, function_call):
        """
        Handle a function call from Gemini.

        Following Gemini docs pattern:
        - Extract name, id, and args from function_call
        - Execute the function (or call handler)
        - Send response back using session.send_tool_response()
        """
        try:
            # Extract function call details
            name = getattr(function_call, 'name', None)
            call_id = getattr(function_call, 'id', None) or str(time.time())
            args = getattr(function_call, 'args', {}) or {}

            if not name:
                self.logger.error("[FunctionCall] Missing function name")
                return

            self.logger.info(f"[FunctionCall] Calling: {name}(id={call_id})")
            if DEBUG == 'true':
                self.logger.debug(f"[FunctionCall] Args: {format_json(args)}")

            # Route to appropriate handler
            if name in self.genesys_tool_handlers:
                # Genesys data action
                await self._handle_genesys_data_action(name, call_id, args)
            else:
                # Call control function
                await self._handle_call_control_function(name, call_id, args)

        except Exception as e:
            self.logger.error(f"[FunctionCall] Error handling function call: {e}", exc_info=True)

    async def _handle_genesys_data_action(self, name: str, call_id: str, args: Dict[str, Any]):
        """
        Execute a Genesys data action and send response to Gemini.

        Following Gemini docs pattern for tool responses:
        session.sendToolResponse({
            functionResponses: [{
                id: fc.id,
                name: fc.name,
                response: { result: "ok" }
            }]
        })
        """
        handler = self.genesys_tool_handlers.get(name)
        if not handler:
            self.logger.error(f"[FunctionCall] No handler for {name}")
            return

        try:
            self.logger.info(f"[FunctionCall] Executing Genesys data action: {name}")

            # Call the handler
            result = await handler(args)

            # Build response payload
            response_payload = {
                "status": "ok",
                "tool": name,
                "result": result or {}
            }

            self.logger.info(f"[FunctionCall] Genesys action {name} completed successfully")

        except Exception as e:
            self.logger.error(f"[FunctionCall] Genesys action {name} failed: {e}", exc_info=True)
            response_payload = {
                "status": "error",
                "tool": name,
                "error_type": type(e).__name__,
                "message": str(e)
            }

        # Send response back to Gemini (as per docs)
        try:
            function_response = types.FunctionResponse(
                id=call_id,
                name=name,
                response=response_payload
            )

            await self.session.send_tool_response(
                function_responses=[function_response]
            )

            self.logger.info(f"[FunctionCall] Sent tool response for {name}")

        except Exception as send_err:
            self.logger.error(f"[FunctionCall] Failed to send tool response: {send_err}", exc_info=True)

    async def _handle_call_control_function(self, name: str, call_id: str, args: Dict[str, Any]):
        """
        Handle call control functions (end_conversation_successfully, end_conversation_with_escalation).

        These functions trigger the call to end, so we:
        1. Send the function response to Gemini
        2. Send a closing instruction to get farewell message
        3. Schedule disconnect after the farewell completes
        """
        try:
            response_payload = {}
            action = None
            info = None
            closing_instruction = None

            if name in ("end_call", "end_conversation_successfully"):
                action = "end_conversation_successfully"
                summary = args.get("summary", "Task completed")
                info = summary

                response_payload = {
                    "result": "ok",
                    "action": action,
                    "summary": summary
                }

                # Schedule disconnect
                self._disconnect_context = {
                    "action": action,
                    "reason": "completed",
                    "info": info
                }
                self._await_disconnect_on_done = True

                # Build closing instruction
                if self.success_prompt:
                    closing_instruction = f'Say this to the customer: "{self.success_prompt}"'
                else:
                    closing_instruction = "Thank the customer briefly and confirm the call is ending."

            elif name in ("handoff_to_human", "end_conversation_with_escalation"):
                action = "end_conversation_with_escalation"
                reason = args.get("reason", "Customer requested agent")
                info = reason

                response_payload = {
                    "result": "ok",
                    "action": action,
                    "reason": reason
                }

                # Schedule disconnect
                self._disconnect_context = {
                    "action": action,
                    "reason": "transfer",
                    "info": info
                }
                self._await_disconnect_on_done = True

                # Build closing instruction
                if self.escalation_prompt:
                    closing_instruction = f'Say this to the customer: "{self.escalation_prompt}"'
                else:
                    closing_instruction = "Let the customer know you're transferring them to an agent."

            else:
                self.logger.warning(f"[FunctionCall] Unknown function: {name}")
                response_payload = {"result": "error", "error": f"Unknown function: {name}"}

            # Send function response
            function_response = types.FunctionResponse(
                id=call_id,
                name=name,
                response=response_payload
            )

            await self.session.send_tool_response(
                function_responses=[function_response]
            )

            self.logger.info(f"[FunctionCall] Sent response for {name}")

            # Send closing instruction to trigger farewell
            if closing_instruction and self._disconnect_context:
                await self.session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=closing_instruction)]
                    ),
                    turn_complete=True
                )

                self.logger.info(
                    f"[FunctionCall] Sent closing instruction. Will disconnect after farewell "
                    f"(action={action})"
                )

        except Exception as e:
            self.logger.error(f"[FunctionCall] Error in call control handler: {e}", exc_info=True)

    async def _handle_disconnect_callback(self):
        """Execute disconnect callback after turn completes."""
        if not self._disconnect_context:
            return

        ctx = self._disconnect_context
        self._await_disconnect_on_done = False
        self._disconnect_context = None

        try:
            action = ctx.get("action")
            reason = ctx.get("reason", "completed")
            info = ctx.get("info", "")

            if action == "end_conversation_successfully":
                if callable(self.on_end_call_request):
                    await self.on_end_call_request(reason, info)

            elif action == "end_conversation_with_escalation":
                if callable(self.on_handoff_request):
                    await self.on_handoff_request("transfer", info)
                elif callable(self.on_end_call_request):
                    await self.on_end_call_request("transfer", info)

        except Exception as e:
            self.logger.error(f"[FunctionCall] Error in disconnect callback: {e}", exc_info=True)

    def _update_token_tracking(self, usage_metadata):
        """Update token usage tracking from Gemini response."""
        try:
            # Gemini provides total counts
            if hasattr(usage_metadata, 'prompt_token_count'):
                self._total_input_tokens = usage_metadata.prompt_token_count

            if hasattr(usage_metadata, 'candidates_token_count'):
                self._total_output_tokens = usage_metadata.candidates_token_count

            # Update detailed breakdown if available
            if hasattr(usage_metadata, 'prompt_tokens_details'):
                details = usage_metadata.prompt_tokens_details
                for detail in details:
                    modality = getattr(detail, 'modality', None)
                    count = getattr(detail, 'token_count', 0)

                    if modality == 'TEXT':
                        self._token_details['input_text'] = count
                    elif modality == 'AUDIO':
                        self._token_details['input_audio'] = count

            if hasattr(usage_metadata, 'response_tokens_details'):
                details = usage_metadata.response_tokens_details
                for detail in details:
                    modality = getattr(detail, 'modality', None)
                    count = getattr(detail, 'token_count', 0)

                    if modality == 'AUDIO':
                        self._token_details['output_audio'] = count

            # Fallback: if no modality breakdown, assume audio for Live API
            if self._token_details['input_audio'] == 0 and self._total_input_tokens > 0:
                self._token_details['input_audio'] = self._total_input_tokens

            if self._token_details['output_audio'] == 0 and self._total_output_tokens > 0:
                self._token_details['output_audio'] = self._total_output_tokens

        except Exception as e:
            self.logger.error(f"Error updating token tracking: {e}", exc_info=True)

    def get_token_metrics(self) -> Dict[str, str]:
        """
        Get token usage metrics for output variables.

        Returns all values as strings for Genesys output variables.
        """
        return {
            "TOTAL_INPUT_TEXT_TOKENS": str(self._token_details.get('input_text', 0)),
            "TOTAL_INPUT_CACHED_TEXT_TOKENS": str(self._token_details.get('input_cached', 0)),
            "TOTAL_INPUT_AUDIO_TOKENS": str(self._token_details.get('input_audio', 0)),
            "TOTAL_INPUT_CACHED_AUDIO_TOKENS": str(0),  # Gemini doesn't provide this separately
            "TOTAL_OUTPUT_TEXT_TOKENS": str(self._token_details.get('output_text', 0)),
            "TOTAL_OUTPUT_AUDIO_TOKENS": str(self._token_details.get('output_audio', 0))
        }

    def register_genesys_tool_handlers(
        self,
        handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]]
    ):
        """Register handlers for Genesys data action tools."""
        self.genesys_tool_handlers = handlers or {}
        if handlers:
            self.logger.info(f"[FunctionCall] Registered {len(handlers)} Genesys tool handler(s)")

    async def _safe_send(self, message: str):
        """
        Compatibility method for OpenAI-style message sending.

        In Gemini, most operations use SDK methods directly instead of JSON messages.
        This exists for interface compatibility but is generally not used.
        """
        # Most Gemini operations use SDK methods, not raw JSON
        if DEBUG == 'true':
            self.logger.debug(f"_safe_send called (Gemini uses SDK methods): {message[:100]}")

    async def handle_rate_limit(self) -> bool:
        """Handle rate limiting with backoff."""
        if self.retry_count >= 3:
            self.logger.error("[Rate Limit] Max retries exceeded")
            return False

        self.retry_count += 1
        delay = GENESYS_RATE_WINDOW * (2 ** (self.retry_count - 1))

        self.logger.warning(
            f"[Rate Limit] Retry {self.retry_count}/3, backing off {delay}s"
        )

        self.running = False
        await asyncio.sleep(delay)
        self.running = True

        self.last_retry_time = time.time()
        return True

    async def terminate_session(self, reason="completed", final_message=None):
        """Terminate the session with optional final message."""
        try:
            if final_message and self.session:
                await self.session.send_client_content(
                    turns=types.Content(
                        role="model",
                        parts=[types.Part(text=final_message)]
                    ),
                    turn_complete=True
                )

            await self.close()
        except Exception as e:
            self.logger.error(f"Error terminating session: {e}", exc_info=True)

    async def await_summary(self, timeout: float = 10.0):
        """
        Generate conversation summary.

        Note: Gemini Live API doesn't have the same summary mechanism as OpenAI.
        We request a summary by sending a specific prompt.
        """
        if not self.session:
            return None

        try:
            # Create future for response
            loop = asyncio.get_event_loop()
            self._summary_future = loop.create_future()

            # Request summary
            await self.session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(
                        text="Provide a brief summary of this conversation in 2-3 sentences."
                    )]
                ),
                turn_complete=True
            )

            # Wait for response
            result = await asyncio.wait_for(self._summary_future, timeout=timeout)
            return result

        except asyncio.TimeoutError:
            self.logger.error("Summary generation timed out")
            return None
        except Exception as e:
            self.logger.error(f"Error generating summary: {e}", exc_info=True)
            return None
        finally:
            self._summary_future = None

    async def disconnect_session(self, reason="completed", info=""):
        """Disconnect the session."""
        await self.close()

    def _start_vad_monitor(self):
        """Start VAD monitor task to flush audio stream on idle."""
        if self._vad_monitor_task and not self._vad_monitor_task.done():
            return

        async def _monitor():
            try:
                while self.running:
                    await asyncio.sleep(VAD_IDLE_CHECK_INTERVAL)

                    # Check if stream is idle
                    if self._audio_stream_open:
                        idle_time = time.monotonic() - self._last_audio_time
                        if idle_time >= 1.0:  # 1 second idle
                            await self._flush_audio_stream()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.logger.error(f"VAD monitor error: {e}", exc_info=True)

        self._vad_monitor_task = asyncio.create_task(_monitor())

    async def close(self):
        """Close the Gemini session and cleanup resources."""
        duration = time.time() - self.start_time
        self.logger.info(f"Closing Gemini session after {duration:.2f}s")

        self.running = False

        # Flush any remaining audio
        self._buffer_and_send_pcmu(b"", flush=True)
        self._pending_pcmu_bytes.clear()
        self._on_audio_callback = None

        # Flush audio stream
        await self._flush_audio_stream()

        # Cancel tasks
        if self._vad_monitor_task:
            self._vad_monitor_task.cancel()
            try:
                await self._vad_monitor_task
            except asyncio.CancelledError:
                pass
            self._vad_monitor_task = None

        if self.read_task:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass
            self.read_task = None

        # Exit session context manager
        if self._session_context_manager:
            try:
                await self._session_context_manager.__aexit__(None, None, None)
                self.logger.debug("Exited Gemini session context")
            except Exception as e:
                self.logger.error(f"Error exiting session context: {e}")
            self._session_context_manager = None

        self.session = None
        self.logger.info("Gemini session closed")
