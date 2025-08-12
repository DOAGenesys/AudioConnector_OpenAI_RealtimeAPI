# Genesys & OpenAI Real-Time Voice Connector

This project provides a real-time voice connector that bridges Genesys Cloud AudioHook with OpenAI's Real-Time API. It enables a voicebot to listen to a live audio stream from a phone call, handle it in real-time, and generate intelligent, human-like responses that can are played back to the caller.

This application is designed to be deployed on cloud platforms like DigitalOcean.

## Features

- **Real-Time Response**: Captures audio from Genesys AudioHook, sends it to OpenAI for live handling, and receives real time voice responses. No need for STT or TTS, it leverages a speech-to-speech LLM.
- **Dynamic AI Configuration**: Customize the AI's behavior directly from Genesys Architect by passing in variables for the system prompt, AI model, voice, and more.
- **Intelligent Conversation Management**: The AI can understand the context of the conversation and provide relevant, helpful responses. It includes built-in turn detection (VAD - Voice Activity Detection) to manage the flow of conversation naturally.
- **Rate Limiting & Error Handling**: Includes robust rate limiting and exponential backoff to handle API limits gracefully and ensure stable operation.
- **Cloud-Ready Deployment**: Stripped of local SSL and port management, making it easy to deploy on modern cloud platforms like DigitalOcean App Platform.

## How It Works

The application operates as a WebSocket server that listens for connections from Genesys Cloud AudioHook.

1. **Connection**: A call in Genesys Cloud with an active AudioHook action initiates a WebSocket connection to this server.
2. **Audio Streaming**: The server receives a real-time stream of the call audio (in PCMU/ULAW format).
3. **AI Processing**: The audio is forwarded to the OpenAI Real-Time API. OpenAI gets the audio, processes it through the specified language model (e.g., gpt-4o-mini-realtime-preview), and generates a response.
4. **Voice Synthesis**: OpenAI synthesizes the response directly into audio using the chosen voice and streams it back.
5. **Playback**: The server sends the synthesized audio back to Genesys, which plays it to the caller.
6. **Summarization & Disconnect**: When the call ends, the application requests a final summary from OpenAI and sends it back to Genesys before closing the connection.

## Getting Started

Follow these instructions to get the project running.

### Prerequisites

- Python 3.9 or higher
- A Genesys Cloud account with the AudioHook integration enabled.
- An OpenAI API key with access to the real-time models.
- A DigitalOcean account (or another cloud provider) for deployment.

### 1. Installation

First, clone the repository to your local machine:

```bash
git clone <your-repository-url>
cd <your-repository-directory>
```

Next, create a Python virtual environment and install the required dependencies.

```bash
# Create a virtual environment
python -m venv venv

# Activate the environment
# On Windows:
# venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

You will need to create a `requirements.txt` file with the following content:

```
websockets
python-dotenv
```

### 2. Configuration

The application is configured using environment variables. Create a `.env` file in the root of the project by copying the example file:

```bash
cp .env.local.example .env
```

Now, edit the `.env` file with your specific settings:

```bash
# .env

# This is the secret key your application will use to validate incoming connections from Genesys.
# You must configure Genesys AudioHook to send this exact value in the 'x-api-key' header.
GENESYS_API_KEY=<your_shared_secret_with_genesys>

# --- OpenAI API Configuration ---
OPENAI_API_KEY=<your_openai_api_key>
OPENAI_MODEL=gpt-4o-mini-realtime-preview

# Voice options for OpenAI:
#   - alloy
#   - ash
#   - ballad
#   - coral
#   - echo
#   - sage
#   - shimmer
#   - verse
OPENAI_VOICE=echo

# --- Debug settings ---
DEBUG=true
```

### Usage

To run the application locally for testing (without SSL), you can use the following command:

```bash
python oai_middleware.py
```

The server will start and listen for WebSocket connections on the default host (0.0.0.0) and port (8080), as defined in `oai_middleware.py`.

## Deployment on DigitalOcean App Platform

This application is optimized for deployment on platforms like DigitalOcean's App Platform, which handles networking, SSL, and scaling.

### Step 1: Push to a Git Repository

Make sure your code, including the `requirements.txt` and `.env` files (for reference, but do not commit your actual secrets), is pushed to a GitHub or GitLab repository.

### Step 2: Create a New App on DigitalOcean

1. Log in to your DigitalOcean account and navigate to the Apps section.
2. Click **Create App** and select your Git repository.
3. DigitalOcean will inspect the code and detect a Python application. It will automatically set the run command.
4. **Run Command**: Ensure the run command is set to `python oai_middleware.py`.

### Step 3: Configure Environment Variables

In the app settings, go to the **Environment Variables** section. Add the secrets from your `.env` file here.

- `OPENAI_API_KEY`: Your OpenAI secret key.
- `OPENAI_MODEL`: (Optional) The default model you want to use.
- `OPENAI_VOICE`: (Optional) The default voice you want to use.
- `DEBUG`: Set to `false` for production.

**Important**: Set these as secret variables to ensure they are encrypted and protected.

### Step 4: Set the HTTP Port

DigitalOcean will expose your application on a specific port. The code is already configured to use the `PORT` environment variable provided by the platform. In the App Spec, ensure the HTTP port is correctly set (e.g., to 8080, which the app will listen on).

### Step 5: Deploy

Save your configuration and deploy the app. DigitalOcean will build the application, install the dependencies from `requirements.txt`, and start the server.

Once deployed, DigitalOcean will provide you with a public URL (e.g., `https://your-app-name-xxxxx.ondigitalocean.app`). This is the URL you will use to configure the AudioHook in Genesys Cloud. The platform automatically handles SSL, so the endpoint will be secure (`wss://`).

## Genesys Cloud Configuration

In your Genesys Cloud you will need an Audio Connector integration and a inbound call Architect flow.

### Audio Connector integration

<img width="903" height="542" alt="image" src="https://github.com/user-attachments/assets/105173df-d334-4f06-b5c8-da29bb1d273e" />

Set the wss endpoint (according to the domain of your middleware):
<img width="1548" height="479" alt="image" src="https://github.com/user-attachments/assets/1529d6e4-9aaf-4d0a-bc9f-e363cf78e64f" />

Set the API Key:

<img width="1182" height="646" alt="image" src="https://github.com/user-attachments/assets/70fb79a4-9d00-4060-9582-1b653f655dd5" />


### Inbound call flow

Example inbound call flow invoking OpenAI Audio connector integration:

<img width="1388" height="1065" alt="image" src="https://github.com/user-attachments/assets/b07d078c-8149-46bd-9d5c-879fe75c252c" />

