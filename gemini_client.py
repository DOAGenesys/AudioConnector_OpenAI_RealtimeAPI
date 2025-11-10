
import asyncio
import json
import time
import base64
import io
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
    GENESYS_RATE_WINDOW
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

        # Gemini client
        self.client = None
        self.model = None

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
        self.model = model if model else "gemini-2.5-flash-native-audio-preview-09-2025"

        try:
            self.logger.info(f"Connecting to Gemini Live API using model: {self.model}...")
            connect_start = time.time()

            # Initialize Gemini client with v1alpha API version
            self.client = genai.Client(
                api_key=self.api_key,
                http_options={'api_version': 'v1alpha'}
            )

            # Build tool declarations for Gemini
            tools = []

            # Add call control tools
            call_control_tools = _default_call_control_tools()
            tools.extend(call_control_tools)

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
                        tools.append(func_def)

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
                )
            )

            config = types.LiveConnectConfig(
                generation_config=generation_config,
                system_instruction=instructions_text,
                tools=tools if tools else None,
            )

            # Connect to Live API
            self.logger.info(f"Initiating Gemini Live API connection with model: {self.model}")
            self.session = await self.client.aio.live.connect(
                model=self.model,
                config=config
            )

            connect_time = time.time() - connect_start
            self.logger.info(
                f"Gemini Live API connection established in {connect_time:.2f}s "
                f"(model={self.model}, voice={self.voice}, temperature={self.temperature})"
            )
            self.running = True

            tool_names = [t.get("name", "unknown") for t in tools]
            self.logger.info(
                f"[FunctionCall] Configured {len(tools)} Gemini tools: {tool_names}"
            )

            self.retry_count = 0

        except Exception as e:
            self.logger.error(f"Error establishing Gemini connection: {e}", exc_info=True)
            self.logger.error(f"Model: {self.model}, Voice: {self.voice}, Temperature: {self.temperature}")
            await self.close()
            raise RuntimeError(f"Failed to connect to Gemini Live API: {str(e)}")

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

                                # Convert PCM16 24kHz to PCMU 8kHz using efficient audioop
                                pcmu_8k = pcm16_24k_to_pcmu_8k(pcm16_24k)
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
            # Update totals (these are cumulative from Gemini)
            if hasattr(usage_metadata, 'total_token_count'):
                total = usage_metadata.total_token_count
            if hasattr(usage_metadata, 'prompt_token_count'):
                self._total_prompt_tokens = usage_metadata.prompt_token_count
            if hasattr(usage_metadata, 'candidates_token_count'):
                self._total_candidates_tokens = usage_metadata.candidates_token_count

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

            self.logger.info(f"[FunctionCall] Sent function response for {name}")

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

            self.logger.info(f"[FunctionCall] Sent Genesys tool response for {name}")
        except Exception as send_exc:
            self.logger.error(f"[FunctionCall] CRITICAL ERROR: Failed to send tool result: {send_exc}", exc_info=True)

    async def close(self):
        """Close the Gemini session."""
        duration = time.time() - self.start_time
        self.logger.info(f"Closing Gemini connection after {duration:.2f}s")
        self.running = False

        if self.session:
            try:
                # Gemini session cleanup happens automatically
                pass
            except Exception as e:
                self.logger.error(f"Error closing Gemini session: {e}")
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
