# Genesys Cloud Real-Time Voice AI Connector

A real-time voice connector that bridges Genesys Cloud Audio Connector with leading AI providers (OpenAI Realtime API and Google Gemini Live API), enabling intelligent voice agent assisted interactions with live phone calls through speech-to-speech processing.

## Overview

This application serves as a WebSocket middleware that captures audio from Genesys Cloud phone calls, processes it through your chosen AI vendor's real-time API (OpenAI or Gemini), and streams intelligent responses back to callers in real-time. Designed for cloud deployment on platforms like DigitalOcean App Platform.

## Features

- **Multi-Vendor AI Support**: Choose between OpenAI Realtime API and Google Gemini Live API based on your needs
- **Real-Time Speech Processing**: Direct speech-to-speech communication without traditional STT/TTS pipeline
- **Dynamic AI Configuration**: Customize AI behavior through Genesys Architect variables (system prompt, model, voice, temperature)
- **Intelligent Conversation Management**: Context-aware responses with built-in Voice Activity Detection (VAD) for natural conversation flow
- **Long Response Support**: Adaptive audio buffering handles 3-minute continuous AI responses without truncation—perfect for detailed explanations, complex lookups, and multi-step function calls
- **Autonomous Call Termination**: AI can autonomously end calls when users indicate they're done or request human escalation using function calling
- **Genesys Data Actions**: Dynamically expose approved Genesys Cloud Data Actions as AI tools so the voice agent can run secure CRM/data lookups in real time
- **Robust Error Handling**: Rate limiting and exponential backoff for stable API interaction
- **Cloud-Ready Architecture**: Optimized for modern cloud platforms with automated SSL and scaling support

## How It Works

1. **Connection Establishment**: Genesys Cloud AudioHook initiates WebSocket connection to the server
2. **Audio Streaming**: Real-time call audio streams in PCMU/ULAW format
3. **AI Processing**: Audio forwarded to your selected AI vendor's real-time API (OpenAI or Gemini) using specified model
4. **Response Generation**: AI vendor processes audio and generates synthesized voice response
5. **Audio Playback**: Synthesized audio streams back through Genesys to caller
6. **Autonomous Call Management**: When appropriate, AI invokes call-control functions (`end_conversation_successfully` or `end_conversation_with_escalation`) and generates a farewell message
7. **Graceful Disconnect**: After farewell audio completes and buffer drains, connector sends disconnect message to Genesys with session outcome data (escalation status, completion summary, token metrics)
8. **Architect Flow Routing**: Architect uses output variables (`ESCALATION_REQUIRED`, `ESCALATION_REASON`, `COMPLETION_SUMMARY`) to route the call appropriately (queue transfer, wrap-up, etc.)

### Audio Format Handling

The connector automatically handles audio format conversion for both providers:
- **Genesys**: PCMU (μ-law) @ 8kHz
- **OpenAI**: PCMU @ 8kHz (native support, no conversion needed)
- **Gemini**: PCM16 @ 16kHz input, PCM16 @ 24kHz output (automatic conversion)

## Prerequisites

- Python 3.9 or higher
- Genesys Cloud account with AudioHook integration enabled
- **One of the following AI vendor credentials:**
  - **OpenAI**: API key with Real-Time API access
  - **Gemini**: Google AI API key with Gemini Live API access
- Cloud deployment platform (DigitalOcean recommended)

## Configuration

### AI Vendor Selection

The connector supports both OpenAI and Gemini AI vendors. Select your vendor using the `AI_VENDOR` environment variable:

```bash
AI_VENDOR=openai  # Use OpenAI Realtime API (default)
# or
AI_VENDOR=gemini  # Use Google Gemini Live API
```

### Common AI Settings (Vendor-Agnostic)

These settings work with both OpenAI and Gemini:

| Variable | Description | Required |
|----------|-------------|----------|
| `AI_MODEL` | AI model to use | No (defaults based on `AI_VENDOR`) |
| `AI_VOICE` | Voice for speech synthesis | No (defaults based on `AI_VENDOR`) |

**Default Models:**
- OpenAI: `gpt-realtime-mini`, `gpt-realtime`
- Gemini: `gemini-2.5-flash-native-audio-preview-09-2025`

**Default Voices:**
- OpenAI: `sage` (options: `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`)
- Gemini: `Kore` (options: `Kore`, `Puck`, `Charon`, `Aoede`, `Fenrir`, `Orbit`, and more)

### Vendor-Specific Configuration

#### OpenAI

| Variable | Description | Required |
|----------|-------------|----------|
| `OPENAI_API_KEY` | Your OpenAI API key | Yes (when `AI_VENDOR=openai`) |

See https://platform.openai.com/docs/guides/realtime-conversations#voice-options for updated OpenAI voice availability.

#### Gemini

| Variable | Description | Required |
|----------|-------------|----------|
| `GEMINI_API_KEY` | Your Google AI API key | Yes (when `AI_VENDOR=gemini`) |

Gemini Live API automatically selects voices based on the language.

See https://ai.google.dev/gemini-api/docs/speech-generation#voices for the complete list of available Gemini voices.

#### Genesys Data Action Environment Variables

Set the following variables when you want the voice agent to call Genesys Cloud Data Actions as part of its function-calling toolkit:

| Variable | Description | Required |
|----------|-------------|----------|
| `GENESYS_CLIENT_ID` / `GENESYS_CLIENT_SECRET` | OAuth client credentials with permission to run the desired data actions. | Yes (when enabled) |
| `GENESYS_REGION` or `GENESYS_BASE_URL` | Region slug (e.g., `usw2.pure.cloud`) or full API base URL. Determines which Genesys org to call. | Yes (when enabled) |
| `GENESYS_LOGIN_URL` | Optional override for the OAuth login URL. Defaults to `https://login.<region>.mypurecloud.com`. | No |
| `GENESYS_ALLOWED_DATA_ACTION_IDS` | Optional comma-separated allowlist enforced server-side. Only IDs in this list are exposed even if the flow requests others. | No |
| `GENESYS_MAX_TOOLS_PER_SESSION` | Caps how many data actions are loaded as tools for a single call (default `10`). | No |
| `GENESYS_MAX_ACTION_CALLS_PER_SESSION` | Limits how many data action invocations the LLM can perform before we block additional calls (default `10`). | No |
| `GENESYS_TOOL_OUTPUT_REDACTION_FIELDS` | Comma-separated JSON paths (e.g., `customer.ssn,account.cardNumber`) that are redacted before returning tool output to the model. | No |

Advanced tuning variables (`GENESYS_HTTP_TIMEOUT_SECONDS`, retry/backoff knobs, etc.) are also available in `config.py` for production hardening.

#### Audio Buffering Configuration

The connector uses an adaptive audio buffering system to handle long AI responses without truncation:

| Setting | Value | Purpose |
|---------|-------|---------|
| `MAX_AUDIO_BUFFER_SIZE` | 1200 frames | Supports ~180 seconds (3 minutes) of continuous audio buffering |
| `AUDIO_BUFFER_WARNING_THRESHOLD_MEDIUM` | 75% | Logs INFO when buffer usage exceeds 75% |
| `AUDIO_BUFFER_WARNING_THRESHOLD_HIGH` | 90% | Logs WARNING when buffer usage exceeds 90% |

**How it works:**
- OpenAI Realtime API sends audio faster than real-time playback speed (per OpenAI documentation)
- The buffer stores incoming frames while respecting Genesys rate limits (10 frames/sec sustained, conservative)
- This prevents audio truncation during long responses (e.g., detailed ticket information, complex explanations)
- Memory usage is minimal (~1.92 MB at full capacity)
- Conservative rate limiting prevents 429 errors and ensures stable connections

**Log messages you may see:**
- **DEBUG**: Normal operation (buffer < 75%)
- **INFO**: Elevated usage, likely a longer response in progress (buffer 75-90%)
- **WARNING**: High usage, extended response (buffer 90%+)
- **ERROR**: Buffer full with dropped frame (should be extremely rare; indicates need for larger buffer)

No configuration required—the system automatically handles responses of any length up to 3 minutes.

#### Remote MCP Tooling

The connector can also expose remote [Model Context Protocol (MCP)](https://platform.openai.com/docs/guides/tools-remote-mcp) servers and OpenAI built-in tools to the Realtime session so the voice agent can call them directly.

1. In Architect, set the **`MCP_TOOLS_JSON` input variable** to a JSON array string. Each entry must follow the OpenAI Realtime spec (e.g., `{"type":"mcp","server_label":"deepwiki","server_url":"https://mcp.deepwiki.com/mcp","require_approval":"never"}` or `{ "type": "web_search_preview" }`). Leave the variable blank to disable MCP for that call.
2. On session start we parse that string, append clear guidance to the system instructions (reminding the model to use `mcp.list_tools`/`mcp_call` events), and register the tools alongside the default call-control and Genesys Data Action functions.
3. The OpenAI client emits structured logs for every `response.mcp_call*` and `mcp_list_tools*` event so MCP activity can be monitored in production.

Because the entries are passed straight through to OpenAI, you can add new MCP servers or built-in tools without code changes—just update the Architect variable and redeploy your flow if needed.

## Deployment

### DigitalOcean App Platform

#### Step 1: DigitalOcean account creation

If you don't have already one, you can get a DigitalOcean account with 200$ in free credits here (and also, you will be helping my R&D efforts!):

<a href="https://www.digitalocean.com/?refcode=e78e0ec0ec1d&utm_campaign=Referral_Invite&utm_medium=Referral_Program&utm_source=badge"><img src="https://web-platforms.sfo2.cdn.digitaloceanspaces.com/WWW/Badge%201.svg" alt="DigitalOcean Referral Badge" /></a>

#### Step 2: Create DigitalOcean App

1. Navigate to DigitalOcean Apps section
2. Click **Create App** and select your repository
3. Define you server specs (for testing purposes you can just select the 5$/month cheapest option)

#### Step 3: Configure Environment Variables

**Provider Selection (Required):**

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `AI_PROVIDER` | AI provider to use | `openai` | `openai` or `gemini` |

| Variable | Description | Example Value |
|----------|-------------|---------------|
| `AI_VENDOR` | AI provider to use (`openai` or `gemini`) | `openai` or `gemini` |
| `OPENAI_API_KEY` | Your OpenAI API key (required if `AI_VENDOR=openai`) | `sk-...` |
| `GEMINI_API_KEY` | Your Google AI API key (required if `AI_VENDOR=gemini`) | Your Gemini API key |
| `GENESYS_API_KEY` | Shared secret from Genesys Audio Connector integration | Your generated API key |
| `GENESYS_CLIENT_ID` | OAuth client ID for Genesys Cloud API access | Your OAuth client ID |
| `GENESYS_CLIENT_SECRET` | OAuth client secret for Genesys Cloud API access | Your OAuth client secret |
| `GENESYS_REGION` | Your Genesys Cloud region | `mypurecloud.com` |

**Optional Environment Variables:**

| Variable | Description | Default Value |
|----------|-------------|---------------|
| `AI_MODEL` | AI model to use (works with both vendors) | Vendor-specific default |
| `AI_VOICE` | Voice for speech synthesis (works with both vendors) | Vendor-specific default |
| `DEBUG` | Enable debug logging | `false` |

**Important**:
- Mark all sensitive variables (API keys, secrets) as **encrypted secrets** in DigitalOcean
- `DEBUG` defaults to `false` if not set (production mode)
- Set `AI_VENDOR` to choose between OpenAI and Gemini
- Only the API key for your selected vendor is required

#### Step 4: Deploy

- Make sure the instance size, the environment variables, the region and the name are correctly set, and proceed ("Create App"):

<img width="976" height="1159" alt="image" src="https://github.com/user-attachments/assets/7a87e31d-6766-4279-aee0-956e26dc23e4" />

#### Step 5: Run command

- The Run command ("python oai_middleware.py") must be set in "Settings", otherwise the deployment will fail:

<img width="898" height="1185" alt="image" src="https://github.com/user-attachments/assets/778872e3-315c-4995-af44-5f7284d95ad8" />

- Use these Health Settings:

  <img width="561" height="680" alt="image" src="https://github.com/user-attachments/assets/433d9dc4-da03-4c2f-b2fe-13a367326ca0" />


- Once the app is deployed, note your deployment URL (e.g., `https://your-app-xxxxx.ondigitalocean.app`):
  

<img width="1438" height="320" alt="image" src="https://github.com/user-attachments/assets/df49e434-3d1f-4870-8cf8-e9770e886415" />



## Genesys Cloud Setup

### Audio Connector Integration

1. Create new Audio Connector integration
2. Configure WebSocket endpoint: `wss://your-app-domain.ondigitalocean.app/audiohook`
3. Set API Key header with your `GENESYS_API_KEY` value
4. Save and activate integration

### Architect Flow Configuration

Create an inbound call flow with the Call Audio Connector action referencing your integration. Example:

<img width="447" height="870" alt="image" src="https://github.com/user-attachments/assets/c32f9173-2459-4b3d-8851-a78dfadf30a0" />

You can import the inbound call flow in this repo (DO - Audio Connector - OAI_v10-0.i3InboundFlow) and use it as a starting point. You will still have to configure all the session variables in the "Call Audio Connector" action, including these two fundamental session variables: DATA_ACTION_IDS and DATA_ACTION_DESCRIPTIONS (see `Session Variables` and `Genesys Data Action Tools` sections below), which will determine agentic tool usage. Also, modify the instruction and guardrail prompts (which are concatenated to form the final system prompt that is sent to OpenAI) according to your use cases.

## Session Variables

### Input Variables (Architect → Connector)

Configure AI behavior by setting these variables before the Call Audio Connector action:

| Variable Name | Description | Default Value |
|---------------|-------------|---------------|
| `AI_SYSTEM_PROMPT` | AI assistant instructions | "You are a helpful assistant." |
| `AI_VOICE` | Voice selection (vendor-agnostic, see options above) | Vendor default |
| `GEMINI_VOICE` | Gemini-specific voice override (only used when `AI_VENDOR=gemini`) | Uses `AI_VOICE` if not set |
| `AI_MODEL` | AI model to use (overrides environment variable) | Uses `AI_MODEL` env var |
| `AI_TEMPERATURE` | Response creativity/randomness (0.0-2.0 for Gemini, 0.6-1.2 for OpenAI) | 0.8 |
| `AI_MAX_OUTPUT_TOKENS` | Maximum tokens in response (may be ignored by some models) | "inf" |
| `LANGUAGE` | Response language override | Not set |
| `CUSTOMER_DATA` | Personalization data (semicolon-separated key:value pairs) | Not set |
| `AGENT_NAME` | AI assistant name for prompts | "AI Assistant" |
| `COMPANY_NAME` | Company name for prompts | "Our Company" |
| `DATA_ACTION_IDS` | Comma/pipe separated Genesys Data Action IDs to expose as realtime tools | Not set |
| `DATA_ACTION_DESCRIPTIONS` | Pipe-delimited descriptions aligned with `DATA_ACTION_IDS` order | Not set |
| `MCP_TOOLS_JSON` | JSON array (as a string) describing MCP/built-in tools to expose. Leave blank to disable. | Not set |

**Legacy Variable Support:** For backward compatibility, the connector also supports the legacy `OPENAI_*` prefixed session variables (`OPENAI_SYSTEM_PROMPT`, `OPENAI_VOICE`, `OPENAI_MODEL`, `OPENAI_TEMPERATURE`, `OPENAI_MAX_OUTPUT_TOKENS`). These will be automatically mapped to their `AI_*` equivalents.

### Output Variables (Connector → Architect)

These variables are returned when the session ends:

| Variable Name | Description |
|---------------|-------------|
| `CONVERSATION_SUMMARY` | JSON-structured call summary (topics, decisions, sentiment) |
| `CONVERSATION_DURATION` | AudioHook session duration in seconds |
| `TOTAL_INPUT_TEXT_TOKENS` | Text tokens sent to the AI vendor |
| `TOTAL_INPUT_CACHED_TEXT_TOKENS` | Cached text tokens sent to the AI vendor |
| `TOTAL_INPUT_AUDIO_TOKENS` | Audio tokens sent to the AI vendor |
| `TOTAL_INPUT_CACHED_AUDIO_TOKENS` | Cached audio tokens sent to the AI vendor |
| `TOTAL_OUTPUT_TEXT_TOKENS` | Text tokens received from the AI vendor |
| `TOTAL_OUTPUT_AUDIO_TOKENS` | Audio tokens received from the AI vendor |
| `ESCALATION_REQUIRED` | `true` when the agent requested a human handoff, else `false` |
| `ESCALATION_REASON` | Explanation captured from the `end_conversation_with_escalation` call |
| `COMPLETION_SUMMARY` | Short summary provided via `end_conversation_successfully` |

**Note:** Some variables (like `AI_MODEL`) are available at both the environment variable level and the session variable level. If different conflicting values are configured, the Genesys session variable will always take precedence.

**Gemini Voice Configuration:** When using Gemini, you can set `GEMINI_VOICE` in the session variables to override the default voice independently of `AI_VOICE`. This allows you to use different voices for OpenAI and Gemini without changing `AI_VOICE`.

## Function Calling for Autonomous Call Management

The middleware exposes both call-control functions and optional Genesys Cloud Data Action tools to the AI model (OpenAI or Gemini). This lets the voice agent terminate calls, escalate to humans, or fetch real customer data without bespoke IVR logic.

### Call-Control Functions

#### `end_conversation_successfully`
Triggered when the caller confirms their request is complete. The model sends a short `summary` describing what was accomplished. The function result is fed back to the AI, which then generates a natural farewell message to the caller. Once this farewell audio completes playing, the connector waits for the audio buffer to fully drain (ensuring the caller hears the entire goodbye), then gracefully disconnects the AudioHook session with `ESCALATION_REQUIRED=false` and `COMPLETION_SUMMARY` containing the provided summary.

#### `end_conversation_with_escalation`
Triggered when the caller explicitly requests a human agent, shows frustration, or the task cannot be completed. The model passes a `reason` explaining why escalation is needed. The function result is sent back to the AI, which generates an appropriate transition message (e.g., "I'll connect you with an agent who can help"). After this message plays completely, the connector disconnects the AudioHook session with `ESCALATION_REQUIRED=true` and `ESCALATION_REASON` populated, allowing Architect to branch into a transfer queue or escalation flow.

Both functions include clear instructions in the system prompt so the model knows exactly when to invoke them. The connector ensures all farewell audio is delivered to the caller before disconnecting, providing a smooth conversational conclusion.

### Genesys Data Action Tools

When `DATA_ACTION_IDS` are provided, the server fetches the corresponding input/success schemas from Genesys Cloud and dynamically builds function tools such as `genesys_data_action_get_account`.

- Each tool mirrors the required/optional fields from the data action input schema, keeping the model honest about arguments.
- Tool output is fed back to the model immediately so it can explain results to the caller in natural language.
- Optional redaction paths prevent sensitive fields from ever reaching the LLM.

#### Configuring Data Action Session Variables

Properly configuring the `DATA_ACTION_IDS` and `DATA_ACTION_DESCRIPTIONS` session variables is critical for enabling your voice agent to leverage Genesys Cloud Data Actions within its function calling toolkit. These variables work in tandem to expose specific data actions to the OpenAI Realtime model and provide the contextual information needed for the AI to understand when and how to use each tool.

##### DATA_ACTION_IDS

This variable contains the unique identifiers of the Genesys Cloud Data Actions you want to expose to the voice agent. Format requirements:

- **Format**: Pipe-separated (`|`) list of data action IDs
- **Structure**: Each ID follows the pattern `category_-_uuid` (e.g., `custom_-_9e9b11f4-ddfc-40d1-a7d3-d24f3815e818`)
- **Order**: The order of IDs must exactly match the order of descriptions in `DATA_ACTION_DESCRIPTIONS`

**Example:**

```
custom_-_9e9b11f4-ddfc-40d1-a7d3-d24f3815e818|custom_-_37be4e2a-805b-4b68-a7df-0fd2768c27b8|custom_-_e4744e9d-e3f2-4ad6-acf1-678717230e25|custom_-_ee93134c-f43e-4278-a5c3-5f98f9dcf4e1
```

##### DATA_ACTION_DESCRIPTIONS

This variable provides natural language descriptions for each data action, helping the AI understand the purpose and appropriate usage context for each tool. Format requirements:

- **Format**: Pipe-separated (`|`) list of descriptions
- **Content**: Clear, concise descriptions explaining what each data action does, including key parameters and use cases
- **Order**: Must align exactly with the order of IDs in `DATA_ACTION_IDS` (first description maps to first ID, second to second, etc.)
- **Quality**: Well-written descriptions directly impact the AI's ability to select the right tool at the right time
- **Input params**: In case you want to hardcode values for any of the input params to be used, just include that part in the description (as I do on my example below for the first data action)

**Example:**

```
Searches knowledge base articles to address general FAQ questions, using semantic query matching with configurable confidence threshold and result limits. Use this to provide RAG responses. For the KBId input param, use dc8de859-0102-4ebd-b216-f3f31e1c78c6, for minConfidence use 0.6 and for maxArticles use 3 |Retrieves complete ticket details including customer information, journey stations, departure schedule, and fare class using ticket reference number|Checks availability for ticket modifications and returns alternative departure times and fare class options with associated change fees|Updates ticket booking with new departure date/time and fare class selections after customer confirmation
```

##### Why These Variables Are Critical

1. **Tool Discovery**: The middleware uses `DATA_ACTION_IDS` to fetch the input/output schemas from Genesys Cloud at session initialization, dynamically constructing OpenAI function definitions

2. **AI Decision-Making**: `DATA_ACTION_DESCRIPTIONS` are embedded in the function definitions sent to OpenAI, directly influencing when and how the model chooses to invoke each tool

3. **Alignment Requirement**: Mismatched order between IDs and descriptions will cause the AI to call the wrong data actions for given scenarios, leading to failed lookups or incorrect data retrieval

4. **Security Boundary**: Only data actions explicitly listed in `DATA_ACTION_IDS` are exposed to the model, providing a clear security perimeter (further enforced by the optional `GENESYS_ALLOWED_DATA_ACTION_IDS` server-side allowlist)

##### Configuration Best Practices

- **Test Individually**: Validate each data action works correctly in Genesys Cloud before adding it to the session variables
- **Descriptive Clarity**: Write descriptions that explain both the what and the when—include trigger phrases customers might use
- **Maintain Order**: Double-check that the nth ID corresponds to the nth description before deploying
- **Iterate on Descriptions**: If the AI isn't selecting the right tool, refine the descriptions rather than modifying the data action itself
- **Use Allowlists**: Set `GENESYS_ALLOWED_DATA_ACTION_IDS` at the environment level to prevent unauthorized data actions from being exposed even if mistakenly included in session variables

##### Complete Working Example

**Architect Flow Configuration:**

Set these variables in your Call Audio Connector action:

```
DATA_ACTION_IDS = custom_-_9e9b11f4-ddfc-40d1-a7d3-d24f3815e818|custom_-_37be4e2a-805b-4b68-a7df-0fd2768c27b8|custom_-_e4744e9d-e3f2-4ad6-acf1-678717230e25|custom_-_ee93134c-f43e-4278-a5c3-5f98f9dcf4e1

DATA_ACTION_DESCRIPTIONS = Searches knowledge base articles to address general FAQ questions, using semantic query matching with configurable confidence threshold and result limits. Use this to provide RAG responses|Retrieves complete ticket details including customer information, journey stations, departure schedule, and fare class using ticket reference number|Checks availability for ticket modifications and returns alternative departure times and fare class options with associated change fees|Updates ticket booking with new departure date/time and fare class selections after customer confirmation
```

**What Happens:**

1. Session starts and the middleware authenticates with Genesys Cloud
2. Four data actions are fetched and registered as OpenAI function tools: `genesys_data_action_search_knowledge`, `genesys_data_action_get_ticket`, `genesys_data_action_check_modification_options`, `genesys_data_action_update_booking`
3. When a caller asks "What's the baggage policy?", the AI invokes `genesys_data_action_search_knowledge` with a semantic query
4. When a caller provides a ticket reference, the AI invokes `genesys_data_action_get_ticket` to retrieve full details
5. All tool outputs are logged, optionally redacted, and fed back to the model for natural language explanation to the caller

### How It Works

1. **Function Detection** – OpenAI Realtime decides whether to call a Genesys data action or a call-control function based on the conversation context.
2. **Secure Execution** – The middleware validates arguments, enforces rate limits, executes the Genesys API call (for data actions), and returns sanitized JSON results to the model.
3. **Graceful Termination** – When a call-control function fires:
   - Function result is sent back to OpenAI
   - OpenAI generates a natural farewell or transition message
   - Audio buffer drains completely, ensuring caller hears the full message
   - Connector issues AudioHook `disconnect` with enriched output variables (`ESCALATION_REQUIRED`, `ESCALATION_REASON`, `COMPLETION_SUMMARY`)
4. **Architect Integration** – Architect can branch on `ESCALATION_REQUIRED`, `ESCALATION_REASON`, or `COMPLETION_SUMMARY` to route calls to queues, wrap-up flows, or closure paths, while still receiving conversation summaries and token metrics.

### Integration Benefits

- **Natural Conversations** – Customers can ask for humans or end calls in their own words.
- **Live Data Access** – Approved Genesys Data Actions are available as first-class tools without embedding credentials inside prompts.
- **Flow-Level Insight** – New output variables expose escalation state and completion summaries for downstream routing.
- **Governed Access** – Server-side allowlists, rate limits, and payload caps prevent runaway tool usage.

---

## Technical Implementation Details

### OpenAI Realtime API Integration

When `AI_VENDOR=openai`, the connector uses the OpenAI Realtime API with the following specifications:

#### Audio Format
- **Input**: PCMU/ULAW 8kHz (from Genesys) → converted internally by OpenAI
- **Output**: PCMU/ULAW 8kHz (to Genesys) ← converted internally by OpenAI
- **Connection**: WebSocket at `wss://api.openai.com/v1/realtime?model={model}`

#### Token Counting (OpenAI)
Token metrics are extracted from the `usage` field in OpenAI's `response.done` events:
```python
usage = response.get("usage", {})
token_details = usage.get("input_token_details", {})
output_details = usage.get("output_token_details", {})
```

Metrics tracked:
- **Input**: `text_tokens`, `audio_tokens` (with `cached_tokens_details` breakdown)
- **Output**: `text_tokens`, `audio_tokens`

#### Function Calling (OpenAI)
- Uses OpenAI's structured function calling format
- Tools are registered in `session.update` with `type: "function"`
- Function responses sent via `conversation.item.create` with `type: "function_call_output"`

#### Model Options
- `gpt-realtime-mini` (recommended for cost-efficiency)
- `gpt-realtime` (higher capability)

---

### Google Gemini Live API Integration

When `AI_VENDOR=gemini`, the connector uses the Gemini Live API with the following specifications:

#### Audio Format
- **Input**: PCMU 8kHz (from Genesys) → resampled to PCM16 16kHz → sent to Gemini
- **Output**: PCM16 24kHz (from Gemini) → resampled to PCM16 8kHz → encoded to PCMU → sent to Genesys
- **Resampling**: Uses `librosa` for high-quality audio resampling
- **Connection**: WebSocket via `google.genai.Client().aio.live.connect()`

#### Token Counting (Gemini)
Token metrics are tracked from `usage_metadata` in Gemini's response messages:
```python
usage_metadata = message.usage_metadata
prompt_tokens = usage_metadata.prompt_token_count
candidates_tokens = usage_metadata.candidates_token_count
```

Detailed breakdown by modality via `response_tokens_details`:
```python
for detail in usage_metadata.response_tokens_details:
    modality = detail.modality  # "TEXT" or "AUDIO"
    count = detail.token_count
```

Metrics tracked:
- **Input**: Estimated from `prompt_token_count` (audio-dominant for voice)
- **Output**: Broken down by `TEXT` and `AUDIO` modalities from `response_tokens_details`

#### Function Calling (Gemini)
- Uses Gemini's native function declaration format
- Tools defined with `name`, `description`, and `parameters` (JSON Schema)
- Function responses sent via `session.send_client_content()` with `FunctionResponse` objects

#### Model Options
- `gemini-2.5-flash-native-audio-preview-09-2025` (default, optimized for voice)
- Supports native audio output with natural speech synthesis
- Features: Affective dialogue, proactive audio, thinking capabilities

#### Voice Options
Gemini automatically selects appropriate voices based on language. Available voices include:
- `Kore` (default), `Puck`, `Charon`, `Aoede`, `Fenrir`, `Orbit`, and many more

See the complete list at: https://ai.google.dev/gemini-api/docs/speech-generation#voices

---

## Choosing Between OpenAI and Gemini

| Feature | OpenAI Realtime API | Gemini Live API |
|---------|---------------------|-----------------|
| **Native Audio Processing** | Yes | Yes (with enhanced quality) |
| **Function Calling** | ✓ | ✓ |
| **Voice Options** | 8 voices | 6+ voices (language-adaptive) |
| **Audio Quality** | High | High (24kHz output) |
| **Latency** | Very Low | Very Low |
| **Token Tracking** | Detailed breakdown | Detailed breakdown |
| **Context Window** | Model-dependent | 128k tokens (native audio models) |
| **Special Features** | - | Affective dialogue, thinking mode |
| **Pricing** | Per model tier | Based on Google AI pricing |

**When to choose OpenAI:**
- You need a well-established API with extensive documentation
- You're already using OpenAI for other services
- You prefer the specific voice characteristics of OpenAI voices

**When to choose Gemini:**
- You want Google's latest multimodal AI capabilities
- You need advanced features like affective dialogue or thinking mode
- You're building within the Google Cloud ecosystem
- You want potentially lower costs with newer models

---
