# Genesys & OpenAI Real-Time Voice Connector

A real-time voice connector that bridges Genesys Cloud Audio Connector with OpenAI's Real-Time API, enabling intelligent voicebot interactions with live phone calls through speech-to-speech processing.

## Overview

This application serves as a WebSocket middleware that captures audio from Genesys Cloud phone calls, processes it through OpenAI's Real-Time API, and streams intelligent responses back to callers in real-time. Designed for cloud deployment on platforms like DigitalOcean App Platform.

## Features

- **Real-Time Speech Processing**: Direct speech-to-speech communication using OpenAI's Real-Time API without traditional STT/TTS pipeline
- **Dynamic AI Configuration**: Customize AI behavior through Genesys Architect variables (system prompt, model, voice, temperature)
- **Intelligent Conversation Management**: Context-aware responses with built-in Voice Activity Detection (VAD) for natural conversation flow
- **Robust Error Handling**: Rate limiting and exponential backoff for stable API interaction
- **Cloud-Ready Architecture**: Optimized for modern cloud platforms with automated SSL and scaling support

## How It Works

1. **Connection Establishment**: Genesys Cloud AudioHook initiates WebSocket connection to the server
2. **Audio Streaming**: Real-time call audio streams in PCMU/ULAW format
3. **AI Processing**: Audio forwarded to OpenAI Real-Time API using specified model (e.g., gpt-4o-mini-realtime-preview)
4. **Response Generation**: OpenAI processes audio and generates synthesized voice response
5. **Audio Playback**: Synthesized audio streams back through Genesys to caller
6. **Session Termination**: Final conversation summary generated and sent to Genesys before connection closure

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

### 3. Create Requirements File

Create `requirements.txt` with the following dependencies:

```txt
websockets
python-dotenv
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
OPENAI_MODEL=gpt-4o-mini-realtime-preview
OPENAI_VOICE=echo

# Debug Settings
DEBUG=true
```

### Available Voice Options

- `alloy`
- `ash`
- `ballad`
- `coral`
- `echo`
- `sage`
- `shimmer`
- `verse`

## Local Development

Start the development server:

```bash
python oai_middleware.py
```

The server will listen on `0.0.0.0:8080` for WebSocket connections.

## Deployment

### DigitalOcean App Platform

#### Step 1: Repository Setup

Push your code to a Git repository (GitHub/GitLab). Include `requirements.txt` but exclude actual secrets from `.env`.

#### Step 2: Create DigitalOcean App

1. Navigate to DigitalOcean Apps section
2. Click **Create App** and select your repository
3. Verify run command is set to: `python oai_middleware.py`

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

#### Step 4: Configure Networking

- Ensure HTTP port is set to 8080
- DigitalOcean automatically handles SSL termination
- Note your deployment URL (e.g., `https://your-app-xxxxx.ondigitalocean.app`)

#### Step 5: Deploy

Save configuration and deploy. DigitalOcean will provide a secure WSS endpoint for Genesys integration.

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
| `OPENAI_MODEL` | OpenAI model to use | "gpt-4o-mini-realtime-preview" |
| `OPENAI_TEMPERATURE` | Response randomness (0.6-1.2) | 0.8 |
| `OPENAI_MAX_OUTPUT_TOKENS` | Max response tokens (integer or "inf") | "inf" |
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

Note: there some variables, like OPENAI_MODEL, that are both available at the environment variable level and also at the session variable level. If different conflicting values are configured, the genesys session variable will always take precedence.
