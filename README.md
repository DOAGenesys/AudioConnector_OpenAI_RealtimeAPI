# Genesys & OpenAI Real-Time Voice Connector

A real-time voice connector that bridges Genesys Cloud Audio Connector with OpenAI's Real-Time API, enabling intelligent voicebot interactions with live phone calls through speech-to-speech processing.

## Overview

This application serves as a WebSocket middleware that captures audio from Genesys Cloud phone calls, processes it through OpenAI's Real-Time API, and streams intelligent responses back to callers in real-time. Designed for cloud deployment on platforms like DigitalOcean App Platform.

## Features

- **Real-Time Speech Processing**: Direct speech-to-speech communication using OpenAI's Real-Time API without traditional STT/TTS pipeline
- **Dynamic AI Configuration**: Customize AI behavior through Genesys Architect variables (system prompt, model, voice, temperature)
- **Intelligent Conversation Management**: Context-aware responses with built-in Voice Activity Detection (VAD) for natural conversation flow
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

## Installation

### 1. Clone Repository

```bash
git clone <your-repository-url>
cd <your-repository-directory>
```

### 2. Create Virtual Environment

```bash
# Create virtual environment
python -m venv venv

# Activate environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Requirements

The project pins `websockets` to the 12.x series to ensure compatibility with the server and OpenAI GA WebSocket interface. Install from the provided file:

```bash
pip install -r requirements.txt
```

## Configuration

### Environment Variables

Copy the example configuration file:

```bash
cp .env.local.example .env
```

Configure your `.env` file:

```bash
# Authentication
GENESYS_API_KEY=<your_shared_secret_with_genesys>

# OpenAI Configuration
OPENAI_API_KEY=<your_openai_api_key>
OPENAI_MODEL=gpt-realtime-mini
OPENAI_VOICE=sage

# Debug Settings
DEBUG=true

# Genesys Data Actions (optional)
ENABLE_GENESYS_DATA_ACTIONS=false
GENESYS_CLIENT_ID=
GENESYS_CLIENT_SECRET=
GENESYS_REGION=
```

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
| `ENABLE_GENESYS_DATA_ACTIONS` | Set to `true` to expose Genesys Data Actions to the model. | No |
| `GENESYS_CLIENT_ID` / `GENESYS_CLIENT_SECRET` | OAuth client credentials with permission to run the desired data actions. | Yes (when enabled) |
| `GENESYS_REGION` or `GENESYS_BASE_URL` | Region slug (e.g., `usw2.pure.cloud`) or full API base URL. Determines which Genesys org to call. | Yes (when enabled) |
| `GENESYS_LOGIN_URL` | Optional override for the OAuth login URL. Defaults to `https://login.<region>.mypurecloud.com`. | No |
| `GENESYS_ALLOWED_DATA_ACTION_IDS` | Optional comma-separated allowlist enforced server-side. Only IDs in this list are exposed even if the flow requests others. | No |
| `GENESYS_MAX_TOOLS_PER_SESSION` | Caps how many data actions are loaded as tools for a single call (default `10`). | No |
| `GENESYS_MAX_ACTION_CALLS_PER_SESSION` | Limits how many data action invocations the LLM can perform before we block additional calls (default `10`). | No |
| `GENESYS_TOOL_OUTPUT_REDACTION_FIELDS` | Comma-separated JSON paths (e.g., `customer.ssn,account.cardNumber`) that are redacted before returning tool output to the model. | No |

Advanced tuning variables (`GENESYS_HTTP_TIMEOUT_SECONDS`, retry/backoff knobs, etc.) are also available in `config.py` for production hardening.

#### Remote MCP Tooling

The connector can also expose remote [Model Context Protocol (MCP)](https://platform.openai.com/docs/guides/tools-remote-mcp) servers and OpenAI built-in tools to the Realtime session so the voice agent can call them directly.

1. Set `ENABLE_MCP_TOOLS=true`.
2. Provide the tool list with either `MCP_TOOLS_JSON='[ ... ]'` or `MCP_SERVERS_CONFIG_PATH=docs/mcp_config.json`. The file must contain a JSON array of tool objects that follow OpenAI's Realtime spec (for example, `{"type":"mcp","server_label":"deepwiki","server_url":"https://mcp.deepwiki.com/mcp","require_approval":"never"}` or `{"type":"web_search_preview"}`).
3. On session start we load the list once, append clear guidance to the system instructions (reminding the model to use `mcp.list_tools`/`mcp_call` events), and register the tools alongside the default call‑control and Genesys Data Action functions.
4. The OpenAI client now emits structured logs for every `response.mcp_call*` and `mcp_list_tools*` event so MCP activity can be monitored in production.

Because the entries are passed straight through to OpenAI, you can add new MCP servers or built-in tools without code changes—just update the JSON blob or file referenced by the environment variables above and redeploy.

## Deployment

### DigitalOcean App Platform

#### Step 1: Repository Setup

Push your code to a Git repository (GitHub/GitLab). Include `requirements.txt` but exclude actual secrets from `.env`.

#### Step 2: Create DigitalOcean App

1. Navigate to DigitalOcean Apps section
2. Click **Create App** and select your repository
3. Define you server specs (for testing purposes you can just select the 5$/month cheapest option)

#### Step 3: Configure Environment Variables

Add the following environment variables in the app settings:

| Variable | Description | Required |
|----------|-------------|----------|
| `OPENAI_API_KEY` | Your OpenAI API key | Yes |
| `GENESYS_API_KEY` | Shared secret for Genesys authentication | Yes |
| `OPENAI_MODEL` | Default OpenAI model | No |
| `OPENAI_VOICE` | Default voice selection | No |
| `DEBUG` | Set to `false` for production | No |

**Important**: Mark sensitive variables as encrypted secrets.

#### Step 4: Deploy

- Make sure the instance size, the environment variables, the region and the name are correctly set, and proceed ("Create App"):

<img width="976" height="1159" alt="image" src="https://github.com/user-attachments/assets/7a87e31d-6766-4279-aee0-956e26dc23e4" />

#### Step 5: Run command

- The Run command ("python oai_middleware.py") must be set in "Settings", otherwise the deployment will fail:

<img width="898" height="1185" alt="image" src="https://github.com/user-attachments/assets/778872e3-315c-4995-af44-5f7284d95ad8" />



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

When `ENABLE_GENESYS_DATA_ACTIONS=true` and `DATA_ACTION_IDS` are provided, the server fetches the corresponding input/success schemas from Genesys Cloud and dynamically builds function tools such as `genesys_data_action_get_account`.

- Each tool mirrors the required/optional fields from the data action input schema, keeping the model honest about arguments.
- Tool output is fed back to the model immediately so it can explain results to the caller in natural language.
- Optional redaction paths prevent sensitive fields from ever reaching the LLM.

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
