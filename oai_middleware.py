import asyncio
import json
import uuid
import logging
import websockets
from websockets.asyncio.server import Response
import http
import os

from config import (
    GENESYS_PATH,
    logger,
    LOG_FILE,
    DEBUG,
    GENESYS_API_KEY
)

from audio_hook_server import AudioHookServer
from utils import format_json, is_websocket_open, get_websocket_path, get_websocket_state_name, extract_request_headers
from datetime import datetime

async def validate_request(connection, request):
    # Extract path and headers from the connection object
    # Use version-agnostic helpers for cross-version compatibility
    path = get_websocket_path(connection)
    request_headers = extract_request_headers(connection, request)

    logger.info(f"\n{'='*50}\n[HTTP] Incoming request validation")
    logger.info(f"[HTTP] Request path: {path}")
    logger.info(f"[HTTP] Expected WebSocket path: {GENESYS_PATH}")

    def build_header_map(source):
        pairs = None
        raw_items = getattr(source, 'raw_items', None)
        if callable(raw_items):
            pairs = list(raw_items())
        else:
            items_fn = getattr(source, 'items', None)
            if callable(items_fn):
                pairs = list(items_fn())
        if pairs is None:
            try:
                pairs = list(source)
            except Exception:
                pairs = []
        return {str(k).lower(): str(v) for k, v in pairs}

    header_keys = build_header_map(request_headers)
    logger.info(f"[HTTP] Remote address: {header_keys.get('host', 'unknown')}")

    logger.info("[HTTP] Full headers received:")
    for name, value in header_keys.items():
        if name in ['x-api-key', 'authorization']:
            logger.info(f"[HTTP]   {name}: {'*' * 8}")
        else:
            logger.info(f"[HTTP]   {name}: {value}")

    upgrade_header = header_keys.get('upgrade', '').lower()
    
    if path == '/' or path == '':
        if upgrade_header != 'websocket':
            logger.info("[HTTP] Health check request detected at root path, returning 200 OK")
            return Response(http.HTTPStatus.OK, b'OK\n')

    if not path.startswith(GENESYS_PATH):
        logger.error("[HTTP] Path mismatch:")
        logger.error(f"[HTTP]   Expected path to start with: {GENESYS_PATH}")
        logger.error(f"[HTTP]   Received path: {path}")
        return Response(http.HTTPStatus.NOT_FOUND, b'Invalid path\n')
    
    logger.info(f"[HTTP] Path validation passed: {path} matches {GENESYS_PATH}")

    # --- Start of Security Update ---
    # Check for the presence and value of the x-api-key
    incoming_api_key = header_keys.get('x-api-key')

    if not incoming_api_key:
        logger.error("[HTTP] Connection rejected - Missing 'x-api-key' header.")
        return Response(http.HTTPStatus.UNAUTHORIZED, b"Missing 'x-api-key' header\n")

    if incoming_api_key != GENESYS_API_KEY:
        logger.error("[HTTP] Connection rejected - Invalid API Key.")
        return Response(http.HTTPStatus.UNAUTHORIZED, b"Invalid API Key\n")

    logger.info("[HTTP] API Key validation successful.")
    # --- End of Security Update ---


    required_headers = [
        'audiohook-organization-id',
        'audiohook-correlation-id',
        'audiohook-session-id',
        'upgrade',
        'sec-websocket-version',
        'sec-websocket-key'
    ]

    missing_headers = []
    found_headers = []
    for h in required_headers:
        if h.lower() not in header_keys:
            missing_headers.append(h)
        else:
            found_headers.append(h)

    if missing_headers:
        error_msg = f"Missing required headers (excluding x-api-key): {', '.join(missing_headers)}"
        logger.error(f"[HTTP] Connection rejected - {error_msg}")
        logger.error("[HTTP] Found headers: " + ", ".join(found_headers))
        return Response(http.HTTPStatus.BAD_REQUEST, error_msg.encode())

    upgrade_header = header_keys.get('upgrade', '').lower()
    logger.info(f"[HTTP] Checking upgrade header: {upgrade_header}")
    if upgrade_header != 'websocket':
        error_msg = f"Invalid upgrade header: {upgrade_header}"
        logger.error(f"[HTTP] {error_msg}")
        return Response(http.HTTPStatus.BAD_REQUEST, b'WebSocket upgrade required\n')

    ws_version = header_keys.get('sec-websocket-version', '')
    logger.info(f"[HTTP] Checking WebSocket version: {ws_version}")
    if ws_version != '13':
        error_msg = f"Invalid WebSocket version: {ws_version}"
        logger.error(f"[HTTP] {error_msg}")
        return Response(http.HTTPStatus.BAD_REQUEST, b'WebSocket version 13 required\n')

    ws_key = header_keys.get('sec-websocket-key')
    if not ws_key:
        logger.error("[HTTP] Missing WebSocket key")
        return Response(http.HTTPStatus.BAD_REQUEST, b'WebSocket key required\n')
    logger.info("[HTTP] Found valid WebSocket key")

    ws_protocol = header_keys.get('sec-websocket-protocol', '')
    if ws_protocol:
        logger.info(f"[HTTP] WebSocket protocol requested: {ws_protocol}")
        if 'audiohook' not in ws_protocol.lower():
            logger.warning("[HTTP] Client didn't request 'audiohook' protocol")

    connection_header = header_keys.get('connection', '').lower()
    logger.info(f"[HTTP] Connection header: {connection_header}")
    if 'upgrade' not in connection_header:
        logger.warning("[HTTP] Connection header doesn't contain 'upgrade'")

    logger.info("[HTTP] All validation checks passed successfully")
    logger.info(f"[HTTP] Proceeding with WebSocket upgrade")
    logger.info("="*50)
    return None

async def handle_genesys_connection(websocket):
    """
    Handles incoming WebSocket connections from Genesys AudioHook service. This function:
    - Creates a unique connection ID for logging
    - Sets up an AudioHookServer instance for the connection
    - Processes incoming messages (both binary audio frames and JSON control messages)
    - Maintains the WebSocket connection until it's closed
    - Handles cleanup when the connection ends

    :param websocket: WebSocket connection object from websockets library
    :return: None
    """
    connection_id = str(uuid.uuid4())[:8]
    logger.info(f"\n{'='*50}\n[WS-{connection_id}] New WebSocket connection handler started")

    session = None

    try:
        logger.info(f"Received WebSocket connection from {websocket.remote_address}")
        logger.info(f"[WS-{connection_id}] Remote address: {websocket.remote_address}")
        logger.info(f"[WS-{connection_id}] Connection state: {websocket.state}")

        # WEBSOCKETS VERSION COMPATIBILITY:
        # 'open' attribute removed from list - doesn't exist in websockets >= 15.0
        # In websockets < 15.0, 'open' was a boolean attribute
        # In websockets >= 15.0, use 'state' enum instead (State.OPEN = 1)
        # Using version-agnostic helper functions from utils.py for path and state
        logger.info(f"[WS-{connection_id}] WebSocket object attributes:")
        logger.info(f"[WS-{connection_id}]   path: {get_websocket_path(websocket)}")
        logger.info(f"[WS-{connection_id}]   remote_address: {getattr(websocket, 'remote_address', 'Not available')}")
        logger.info(f"[WS-{connection_id}]   local_address: {getattr(websocket, 'local_address', 'Not available')}")
        logger.info(f"[WS-{connection_id}]   state: {get_websocket_state_name(websocket)}")
        logger.info(f"[WS-{connection_id}]   protocol: {getattr(websocket, 'protocol', 'Not available')}")
        # Use backward-compatible helper to check if websocket is open
        # Works with both websockets >= 15.0 (state enum) and < 15.0 (open boolean)
        logger.info(f"[WS-{connection_id}]   is_open: {is_websocket_open(websocket)}")

        logger.info(f"[WS-{connection_id}] WebSocket connection established; handshake was validated beforehand.")

        # Create a new AudioHookServer instance for this connection
        session = AudioHookServer(websocket)
        logger.info(f"[WS-{connection_id}] Session created with ID: {session.session_id}")

        logger.info(f"[WS-{connection_id}] Starting main message loop")

        # Handle messages until the connection is closed
        # The websocket receives either binary frames or JSON messages
        while session.running:
            try:
                logger.debug(f"[WS-{connection_id}] Waiting for next message...")
                msg = await websocket.recv()
                # Check for binary frame
                if isinstance(msg, bytes):
                    # Handle binary frame
                    logger.debug(f"[WS-{connection_id}] Received binary frame: {len(msg)} bytes")
                    await session.handle_audio_frame(msg)
                else:
                    try:
                        # Handle JSON message by first parsing it as JSON and then calling the handle_message method
                        data = json.loads(msg)
                        logger.debug(f"[WS-{connection_id}] Received JSON message:\n{format_json(data)}")
                        await session.handle_message(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"[WS-{connection_id}] Error parsing JSON: {e}")
                        await session.disconnect_session("error", f"JSON parse error: {e}")
                    # TODO: Are there specific exceptions that need to be handled here? Especially from the method handle_message?
                    except Exception as e:
                        logger.error(f"[WS-{connection_id}] Error processing message: {e}")
                        await session.disconnect_session("error", f"Message processing error: {e}")
            except websockets.ConnectionClosed as e:
                # Connection closed normally or by client/server
                logger.info(f"[WS-{connection_id}] Connection closed: code={e.code}, reason={e.reason}")
                await session.disconnect_session("closed", f"Connection closed: {e.reason}")
                break
            except Exception as e:
                # Catch any other unexpected errors and log them
                logger.error(f"[WS-{connection_id}] Unexpected error: {e}", exc_info=True)
                break

        logger.info(f"[WS-{connection_id}] Session loop ended, cleaning up")
        # Close the session and cleanup any open connections
        if session and session.openai_client:
            await session.openai_client.close()
        logger.info(f"[WS-{connection_id}] Session cleanup complete")

    except Exception as e:
        logger.error(f"[WS-{connection_id}] Fatal connection error: {e}", exc_info=True)
        if session is None:
            session = AudioHookServer(websocket)
        await session.disconnect_session(reason="error", info=f"Internal error: {str(e)}")
    finally:
        logger.info(f"[WS-{connection_id}] Connection handler finished\n{'='*50}")

async def main():
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8080))
    
    startup_msg = f"""
{'='*80}
Genesys-OpenAI Bridging Server
Starting up at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Host: {host}
Port: {port}
Path: {GENESYS_PATH}
SSL: Managed by deployment platform
Log File: {os.path.abspath(LOG_FILE)}
{'='*80}
"""
    logger.info(startup_msg)

    websockets_logger = logging.getLogger('websockets')
    if DEBUG != 'true':
        websockets_logger.setLevel(logging.INFO)

    websockets_logger.addHandler(logging.FileHandler(LOG_FILE))

    try:
        async with websockets.serve(
            handle_genesys_connection,
            host,
            port,
            process_request=validate_request, # Use the updated validation function
            max_size=64000,
            ping_interval=None,
            ping_timeout=None
        ):
            logger.info(
                f"Server is listening for Genesys AudioHook connections on "
                f"ws://{host}:{port}{GENESYS_PATH}"
            )
            
            try:
                await asyncio.Future()  # run forever
            except asyncio.CancelledError:
                logger.info("Server shutdown initiated")
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down via KeyboardInterrupt.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        logger.info("Server shutdown complete.")
