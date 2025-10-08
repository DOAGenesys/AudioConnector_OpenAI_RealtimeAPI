import asyncio
import json
import uuid
import logging
import websockets
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
from utils import format_json
from datetime import datetime

async def validate_request(connection, request):
    """
    This function is updated to correctly handle the connection and request
    objects passed by the websockets library.
    """
    path = connection.path
    headers = request.headers

    logger.info(f"\n{'='*50}\n[HTTP] Starting WebSocket upgrade validation")
    logger.info(f"[HTTP] Target path: {path}")
    logger.info(f"[HTTP] Remote address: {headers.get('Host', 'unknown')}")

    # Handle health checks from the deployment platform
    if path == "/":
        logger.info("[HTTP] Health check request received. Responding with 200 OK.")
        return http.HTTPStatus.OK, [], b"OK\n"

    logger.info("[HTTP] Full headers received:")
    for name, value in headers.items():
        if name.lower() in ['x-api-key', 'authorization']:
            logger.info(f"[HTTP]   {name}: {'*' * 8}")
        else:
            logger.info(f"[HTTP]   {name}: {value}")

    normalized_path = path.rstrip('/')
    normalized_target = GENESYS_PATH.rstrip('/')

    if normalized_path != normalized_target:
        logger.error("[HTTP] Path mismatch:")
        logger.error(f"[HTTP]   Expected: {GENESYS_PATH}")
        logger.error(f"[HTTP]   Normalized received: {normalized_path}")
        logger.error(f"[HTTP]   Normalized expected: {normalized_target}")
        return http.HTTPStatus.NOT_FOUND, [], b'Invalid path\n'

    # --- Start of Security Update ---
    # Check for the presence and value of the x-api-key
    header_keys = {k.lower(): v for k, v in headers.items()}
    incoming_api_key = header_keys.get('x-api-key')

    if not incoming_api_key:
        logger.error("[HTTP] Connection rejected - Missing 'x-api-key' header.")
        return http.HTTPStatus.UNAUTHORIZED, [], b"Missing 'x-api-key' header\n"

    if incoming_api_key != GENESYS_API_KEY:
        logger.error("[HTTP] Connection rejected - Invalid API Key.")
        return http.HTTPStatus.UNAUTHORIZED, [], b"Invalid API Key\n"

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
        return http.HTTPStatus.BAD_REQUEST, [], error_msg.encode()

    upgrade_header = header_keys.get('upgrade', '').lower()
    logger.info(f"[HTTP] Checking upgrade header: {upgrade_header}")
    if upgrade_header != 'websocket':
        error_msg = f"Invalid upgrade header: {upgrade_header}"
        logger.error(f"[HTTP] {error_msg}")
        return http.HTTPStatus.BAD_REQUEST, [], b'WebSocket upgrade required\n'

    ws_version = header_keys.get('sec-websocket-version', '')
    logger.info(f"[HTTP] Checking WebSocket version: {ws_version}")
    if ws_version != '13':
        error_msg = f"Invalid WebSocket version: {ws_version}"
        logger.error(f"[HTTP] {error_msg}")
        return http.HTTPStatus.BAD_REQUEST, [], b'WebSocket version 13 required\n'

    ws_key = header_keys.get('sec-websocket-key')
    if not ws_key:
        logger.error("[HTTP] Missing WebSocket key")
        return http.HTTPStatus.BAD_REQUEST, [], b'WebSocket key required\n'
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
    connection_id = str(uuid.uuid4())[:8]
    logger.info(f"\n{'='*50}\n[WS-{connection_id}] New WebSocket connection handler started")

    session = None

    try:
        logger.info(f"Received WebSocket connection from {websocket.remote_address}")
        logger.info(f"[WS-{connection_id}] Remote address: {websocket.remote_address}")
        logger.info(f"[WS-{connection_id}] Connection state: {websocket.state}")

        ws_attributes = ['path', 'remote_address', 'local_address', 'state', 'open', 'protocol']
        logger.info(f"[WS-{connection_id}] WebSocket object attributes:")
        for attr in ws_attributes:
            value = getattr(websocket, attr, "Not available")
            logger.info(f"[WS-{connection_id}]   {attr}: {value}")

        logger.info(f"[WS-{connection_id}] WebSocket connection established; handshake was validated beforehand.")

        session = AudioHookServer(websocket)
        logger.info(f"[WS-{connection_id}] Session created with ID: {session.session_id}")

        logger.info(f"[WS-{connection_id}] Starting main message loop")
        while session.running:
            try:
                logger.debug(f"[WS-{connection_id}] Waiting for next message...")
                msg = await websocket.recv()
                if isinstance(msg, bytes):
                    logger.debug(f"[WS-{connection_id}] Received binary frame: {len(msg)} bytes")
                    await session.handle_audio_frame(msg)
                else:
                    try:
                        data = json.loads(msg)
                        logger.debug(f"[WS-{connection_id}] Received JSON message:\n{format_json(data)}")
                        await session.handle_message(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"[WS-{connection_id}] Error parsing JSON: {e}")
                        await session.disconnect_session("error", f"JSON parse error: {e}")
                    except Exception as e:
                        logger.error(f"[WS-{connection_id}] Error processing message: {e}")
                        await session.disconnect_session("error", f"Message processing error: {e}")

            except websockets.ConnectionClosed as e:
                logger.info(f"[WS-{connection_id}] Connection closed: code={e.code}, reason={e.reason}")
                break
            except Exception as e:
                logger.error(f"[WS-{connection_id}] Unexpected error: {e}", exc_info=True)
                break

        logger.info(f"[WS-{connection_id}] Session loop ended, cleaning up")
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

def select_subprotocol(request, available_subprotocols):
    """
    Selects a subprotocol to use for the WebSocket connection.
    """
    requested = request.headers.get("Sec-WebSocket-Protocol")
    if requested:
        logger.info(f"[HTTP] Client requested subprotocols: {requested}")
        # Split and strip whitespace
        protocols = [p.strip() for p in requested.split(',')]
        for p in protocols:
            if p in available_subprotocols:
                logger.info(f"[HTTP] Selected subprotocol: {p}")
                return p
    logger.info("[HTTP] No matching subprotocol found or none requested.")
    return None


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
            process_request=validate_request,
            select_subprotocol=select_subprotocol,
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
