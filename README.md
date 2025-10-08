# Genesys & OpenAI Real-Time Voice Connector

A real-time voice connector that bridges Genesys Cloud Audio Connector with OpenAI's Real-Time API, enabling intelligent voicebot interactions with live phone calls through speech-to-speech processing.

## Overview

This application serves as a WebSocket middleware that captures audio from Genesys Cloud phone calls, processes it through OpenAI's Real-Time API, and streams intelligent responses back to callers in real-time. Designed for cloud deployment on platforms like DigitalOcean App Platform.

## Features

- **Real-Time Speech Processing**: Direct speech-to-speech communication using OpenAI's Real-Time API without traditional STT/TTS pipeline
- **Dynamic AI Configuration**: Customize AI behavior through Genesys Architect variables (system prompt, model, voice, temperature)
- **Intelligent Conversation Management**: Context-aware responses with built-in Voice Activity Detection (VAD) for natural conversation flow
- **Autonomous Call Termination**: AI can autonomously end calls when users indicate they're done or request human escalation using OpenAI function calling
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

Note: there are some variables, like OPENAI_MODEL, that are both available at the environment variable level and also at the session variable level. If different conflicting values are configured, the genesys session variable will always take precedence.

## Function Calling for Autonomous Call Management

[THIS FEATURE HAS TO BE POLISHED AND TESTED, BY NOW THE OPENAI AGENT FAILS TO AUTONOMOUSLY DISCONNECT THE CALL]

The connector leverages OpenAI's function calling capability to enable the AI to autonomously manage call lifecycle based on user intent. This allows for more natural conversation flow and reduces the need for manual call termination logic.

### Available Functions

The AI can call two functions during conversations:

#### `end_call`
Triggers when the user indicates their request is complete or they're finished with the conversation.

**Example phrases that trigger `end_call`:**
- "Ok, I'm done for today, thank you."
- "That is everything, bye"
- "I'm all set, thanks"

**Function parameters:**
- `reason` (string): Short reason for ending the call
- `note` (string, optional): Additional context or notes

#### `handoff_to_human`
Triggers when the user requests to speak with a human agent or indicates they need human assistance.

**Example phrases that trigger `handoff_to_human`:**
- "I want to speak to a person"
- "Put me through to a human agent"
- "Can I talk to someone?"

**Function parameters:**
- `reason` (string): Why the caller wants a human
- `department` (string, optional): Target department or queue

### How It Works

1. **Function Detection**: OpenAI's model analyzes user input and determines if it should call a function
2. **Function Execution**: The server receives the function call arguments and executes the appropriate action
3. **Graceful Termination**: The AI provides a brief farewell message, then the server initiates Genesys call termination
4. **Architect Integration**: The disconnect reason is passed back to Genesys Architect for flow branching

### Integration Benefits

- **Natural Conversations**: Users can end calls conversationally without specific keywords
- **Intelligent Escalation**: Automatic detection of human agent requests
- **Architect Flow Control**: Different disconnect reasons allow for conditional branching in Architect flows
- **Reduced Manual Logic**: Less need for complex timeout or keyword-based termination rules
