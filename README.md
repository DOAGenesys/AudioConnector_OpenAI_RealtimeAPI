# Genesys & OpenAI Real-Time Voice Connector

A real-time voice connector that bridges Genesys Cloud Audio Connector with OpenAI's Real-Time API, enabling intelligent voicebot interactions with live phone calls through speech-to-speech processing.

## Overview

This application serves as a WebSocket middleware that captures audio from Genesys Cloud phone calls, processes it through OpenAI's Real-Time API, and streams intelligent responses back to callers in real-time. Designed for cloud deployment on platforms like DigitalOcean App Platform.

## Features

- **Real-Time Speech Processing**: Direct speech-to-speech communication using OpenAI's Real-Time API without traditional STT/TTS pipeline
- **Dynamic AI Configuration**: Customize AI behavior through Genesys Architect variables (system prompt, model, voice, temperature)
- **Intelligent Conversation Management**: Context-aware responses with built-in Voice Activity Detection (VAD) for natural conversation flow
- **Long Response Support**: Adaptive audio buffering handles 3-minute continuous AI responses without truncation—perfect for detailed explanations, complex lookups, and multi-step function calls
- **Autonomous Call Termination**: AI can autonomously end calls when users indicate they're done or request human escalation using OpenAI function calling
- **Genesys Data Actions**: Dynamically expose approved Genesys Cloud Data Actions as OpenAI tools so the voice agent can run secure CRM/data lookups in real time
- **Robust Error Handling**: Rate limiting and exponential backoff for stable API interaction
- **Cloud-Ready Architecture**: Optimized for modern cloud platforms with automated SSL and scaling support

## How It Works

1. **Connection Establishment**: Genesys Cloud AudioHook initiates WebSocket connection to the server
2. **Audio Streaming**: Real-time call audio streams in PCMU/ULAW format
3. **AI Processing**: Audio forwarded to OpenAI Real-Time API using specified model (e.g., gpt-realtime-mini)
4. **Response Generation**: OpenAI processes audio and generates synthesized voice response
5. **Autonomous Call Management**: AI uses function calling to autonomously end calls when users indicate completion or request human escalation
6. **Audio Playback**: Synthesized audio streams back through Genesys to caller
7. **Session Termination**: Final conversation summary generated and sent to Genesys before connection closure

## Prerequisites

- Python 3.9 or higher
- Genesys Cloud account with AudioHook integration enabled
- OpenAI API key with Real-Time API access
- Cloud deployment platform (DigitalOcean recommended)

## Configuration

### Available OpenAI Voice Options:

- `alloy`
- `ash`
- `ballad`
- `coral`
- `echo`
- `sage`
- `shimmer`
- `verse`

See https://platform.openai.com/docs/guides/realtime-conversations#voice-options for updated voice availability.

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

**Minimum Mandatory Environment Variables:**

These are the **required** environment variables you must set for the integration to work:

| Variable | Description | Example Value |
|----------|-------------|---------------|
| `OPENAI_API_KEY` | Your OpenAI API key | `sk-...` |
| `OPENAI_MODEL` | OpenAI model to use | `gpt-realtime` (recommended) |
| `GENESYS_API_KEY` | Shared secret from Genesys Audio Connector integration | Your generated API key |
| `GENESYS_CLIENT_ID` | OAuth client ID for Genesys Cloud API access | Your OAuth client ID |
| `GENESYS_CLIENT_SECRET` | OAuth client secret for Genesys Cloud API access | Your OAuth client secret |
| `GENESYS_REGION` | Your Genesys Cloud region | `mypurecloud.com` |

**Optional Environment Variables:**

| Variable | Description | Default Value |
|----------|-------------|---------------|
| `OPENAI_VOICE` | Voice selection for AI responses | `sage` |
| `DEBUG` | Enable debug logging | `false` |

**Important**: 
- Mark all sensitive variables (API keys, secrets) as **encrypted secrets** in DigitalOcean
- `DEBUG` defaults to `false` if not set (production mode)
- Use `gpt-realtime` as the model for optimal performance and cost

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

<img width="1389" height="1076" alt="image" src="https://github.com/user-attachments/assets/7c57d40a-81f0-4afc-b5ee-ed12996a886f" />

You can import the inbound call flow in this repo (DO - Audio Connector - OAI_v8-0.i3InboundFlow) and use it as a starting point. You will still have to configure all the session variables in the "Call Audio Connector" action, including these two fundamental session variables: DATA_ACTION_IDS and DATA_ACTION_DESCRIPTIONS (see `Session Variables` and `Genesys Data Action Tools` sections below), which will determine agentic tool usage. Also, modify the instruction and guardrail prompts (which are concatenated to form the final system prompt that is sent to OpenAI) according to your use cases.

## Session Variables

### Input Variables (Architect → Connector)

Configure AI behavior by setting these variables before the Call Audio Connector action:

| Variable Name | Description | Default Value |
|---------------|-------------|---------------|
| `OPENAI_SYSTEM_PROMPT` | AI assistant instructions | "You are a helpful assistant." |
| `OPENAI_VOICE` | Voice selection (see options above) | "sage" |
| `OPENAI_MODEL` | OpenAI model to use | "gpt-realtime-mini" |
| `OPENAI_TEMPERATURE` | Deprecated; ignored by GA Realtime API | — |
| `OPENAI_MAX_OUTPUT_TOKENS` | Deprecated; ignored by GA Realtime API | — |
| `LANGUAGE` | Response language override | Not set |
| `CUSTOMER_DATA` | Personalization data (semicolon-separated key:value pairs) | Not set |
| `AGENT_NAME` | AI assistant name for prompts | "AI Assistant" |
| `COMPANY_NAME` | Company name for prompts | "Our Company" |
| `DATA_ACTION_IDS` | Comma/pipe separated Genesys Data Action IDs to expose as realtime tools | Not set |
| `DATA_ACTION_DESCRIPTIONS` |`-delimited descriptions aligned with `DATA_ACTION_IDS` order | Not set |
| `MCP_TOOLS_JSON` | JSON array (as a string) describing MCP/built-in tools to expose. Leave blank to disable. | Not set |

### Output Variables (Connector → Architect)

These variables are returned when the session ends:

| Variable Name | Description |
|---------------|-------------|
| `CONVERSATION_SUMMARY` | JSON-structured call summary (topics, decisions, sentiment) |
| `CONVERSATION_DURATION` | AudioHook session duration in seconds |
| `TOTAL_INPUT_TEXT_TOKENS` | Text tokens sent to OpenAI |
| `TOTAL_INPUT_CACHED_TEXT_TOKENS` | Cached text tokens sent to OpenAI |
| `TOTAL_INPUT_AUDIO_TOKENS` | Audio tokens sent to OpenAI |
| `TOTAL_INPUT_CACHED_AUDIO_TOKENS` | Cached audio tokens sent to OpenAI |
| `TOTAL_OUTPUT_TEXT_TOKENS` | Text tokens received from OpenAI |
| `TOTAL_OUTPUT_AUDIO_TOKENS` | Audio tokens received from OpenAI |
| `ESCALATION_REQUIRED` | `true` when the agent requested a human handoff, else `false` |
| `ESCALATION_REASON` | Explanation captured from the `end_conversation_with_escalation` call |
| `COMPLETION_SUMMARY` | Short summary provided via `end_conversation_successfully` |

Note: there are some variables, like OPENAI_MODEL, that are both available at the environment variable level and also at the session variable level. If different conflicting values are configured, the genesys session variable will always take precedence.

## Function Calling for Autonomous Call Management

The middleware now exposes both call-control functions and optional Genesys Cloud Data Action tools to the OpenAI Realtime model. This lets the voice agent terminate calls, escalate to humans, or fetch real customer data without bespoke IVR logic.

### Call-Control Functions

#### `end_conversation_successfully`
Triggered when the caller confirms their request is complete. The model sends a short `summary` describing what was accomplished and the server gracefully disconnects the AudioHook session after the agent delivers a closing line.

#### `end_conversation_with_escalation`
Triggered when the caller asks for a human, becomes frustrated, or the task cannot continue. The model passes a `reason`, we log it, and the connector returns `ESCALATION_REQUIRED=true` plus the reason so Architect can branch into a transfer queue.

Both functions include straightforward instructions inside the system prompt so the model knows exactly when to call them.

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

**Example:**

```
Searches knowledge base articles to address general FAQ questions, using semantic query matching with configurable confidence threshold and result limits. Use this to provide RAG responses|Retrieves complete ticket details including customer information, journey stations, departure schedule, and fare class using ticket reference number|Checks availability for ticket modifications and returns alternative departure times and fare class options with associated change fees|Updates ticket booking with new departure date/time and fare class selections after customer confirmation
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

1. **Function Detection** – OpenAI Realtime decides whether to call a Genesys data action or a call-control function.
2. **Secure Execution** – The middleware validates arguments, enforces rate limits, executes the Genesys API call, and returns sanitized JSON results to the model.
3. **Graceful Termination** – When a call-control function fires, the agent plays a brief acknowledgment before the connector issues the appropriate AudioHook `disconnect` with enriched output variables.
4. **Architect Integration** – Architect can branch on `ESCALATION_REQUIRED`, `ESCALATION_REASON`, or `COMPLETION_SUMMARY`, while still receiving the usual conversation summary and token metrics.

### Integration Benefits

- **Natural Conversations** – Customers can ask for humans or end calls in their own words.
- **Live Data Access** – Approved Genesys Data Actions are available as first-class tools without embedding credentials inside prompts.
- **Flow-Level Insight** – New output variables expose escalation state and completion summaries for downstream routing.
- **Governed Access** – Server-side allowlists, rate limits, and payload caps prevent runaway tool usage.
