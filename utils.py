import json
import audioop
import re

from config import (
    MASTER_SYSTEM_PROMPT,
    LANGUAGE_SYSTEM_PROMPT,
    logger
)

def is_websocket_open(ws) -> bool:
    """
    Backward-compatible check for websocket open state.
    Works with both websockets 15.x (state enum) and older versions (open attribute).

    WEBSOCKETS VERSION COMPATIBILITY:
    - websockets >= 15.0: Removed 'open' boolean attribute, use State enum instead
      * ws.state == State.OPEN checks if connection is open
      * State enum values: CONNECTING(0), OPEN(1), CLOSING(2), CLOSED(3)

    - websockets < 15.0: Used 'open' boolean attribute
      * ws.open returns True if the connection is open, False otherwise

    This function tries the new approach first, then falls back to old approach
    for maximum compatibility across all websocket library versions.

    :param ws: WebSocket connection object
    :return: True if websocket is open, False otherwise
    """
    if ws is None:
        return False

    # Try websockets 15.x+ approach (state enum)
    # The 'open' attribute was removed in v15.0, replaced with state enum
    try:
        from websockets.protocol import State
        if hasattr(ws, 'state'):
            return ws.state == State.OPEN
    except (ImportError, AttributeError):
        pass

    # Fall back to older websockets versions (< 15.0) using 'open' boolean attribute
    if hasattr(ws, 'open'):
        return ws.open

    # If neither method works, assume closed for safety
    return False

def get_websocket_path(ws) -> str:
    """
    Backward-compatible method to extract WebSocket path.
    Works across different websockets library versions and object types.

    WEBSOCKETS VERSION COMPATIBILITY:
    - websockets 12.x: In process_request callback, the first parameter IS the path string
    - websockets >= 13.0: In connection handlers, ws.request.path nested attribute
    - websockets < 12.0: Path may be directly on ws.path

    This function tries multiple approaches to retrieve the path
    for maximum compatibility across all websocket library versions.

    :param ws: Path string (12.x), WebSocket connection object, OR Request object
    :return: Path string if available, otherwise "Not available"
    """
    if ws is None:
        return "Not available"

    # In websockets 12.x, process_request receives the path as a string directly
    # Check if the parameter is already a string (the path itself)
    if isinstance(ws, str):
        return ws

    # Try the direct path attribute first (Request object passed from process_request)
    # This handles the case where ws is actually a Request object, not ServerConnection
    # Request objects have 'path' but no 'state' attribute
    # ServerConnection objects have 'state' but path is nested under 'request'
    if hasattr(ws, 'path') and not hasattr(ws, 'state'):
        path = getattr(ws, 'path', None)
        if path is not None and isinstance(path, str):
            return path

    # Try ws.request.path (websockets 13.x+ for ServerConnection in connection handlers)
    if hasattr(ws, 'request') and hasattr(ws.request, 'path'):
        return ws.request.path

    # Try the direct ws.path attribute (older versions where path was on connection object)
    if hasattr(ws, 'path'):
        path = getattr(ws, 'path', None)
        if path is not None and isinstance(path, str):
            return path

    # Could not determine a path
    return "Not available"

def get_websocket_state_name(ws) -> str:
    """
    Get a human-readable WebSocket state name.
    Works across different websockets library versions.

    WEBSOCKETS VERSION COMPATIBILITY:
    - websockets >= 15.0: Uses State enum (CONNECTING=0, OPEN=1, CLOSING=2, CLOSED=3)
    - websockets < 15.0: May have 'open' boolean or numeric state

    :param ws: WebSocket connection object
    :return: State name string (e.g., "OPEN (1)", "CLOSED (3)", etc.)
    """
    if ws is None:
        return "UNKNOWN"

    # Try to get state value
    state_value = getattr(ws, 'state', None)

    if state_value is not None:
        # Try to import State enum for name lookup
        try:
            from websockets.protocol import State
            state_names = {
                State.CONNECTING: "CONNECTING",
                State.OPEN: "OPEN",
                State.CLOSING: "CLOSING",
                State.CLOSED: "CLOSED"
            }
            # If state_value is an enum member, get its name
            if hasattr(state_value, 'name'):
                return f"{state_value.name} ({state_value.value})"
            # Otherwise treat as integer
            elif isinstance(state_value, int):
                # Map integer to name
                int_to_name = {0: "CONNECTING", 1: "OPEN", 2: "CLOSING", 3: "CLOSED"}
                name = int_to_name.get(state_value, "UNKNOWN")
                return f"{name} ({state_value})"
        except (ImportError, AttributeError):
            pass

        # Fallback: just return the numeric value
        if isinstance(state_value, int):
            int_to_name = {0: "CONNECTING", 1: "OPEN", 2: "CLOSING", 3: "CLOSED"}
            name = int_to_name.get(state_value, "UNKNOWN")
            return f"{name} ({state_value})"

        return str(state_value)

    # Fall back to 'open' attribute for older versions
    if hasattr(ws, 'open'):
        return "OPEN" if ws.open else "CLOSED"

    return "UNKNOWN"

def extract_request_headers(connection, request):
    """
    Extract request headers from process_request callback parameters.
    Works across different websockets library versions.

    WEBSOCKETS VERSION COMPATIBILITY:
    - websockets 12.x: process_request(path: str, request_headers: Headers)
      * First param is the path string
      * Second param is the Headers object directly

    - websockets 13.x-14.x: process_request(connection: ServerConnection, request: Request)
      * First param is ServerConnection object
      * Second param is Request object with request.headers

    - websockets >= 15.0: process_request(connection: ServerConnection)
      * Only one param - ServerConnection object
      * Headers accessed via connection.request.headers

    :param connection: First parameter from process_request callback (path string or ServerConnection)
    :param request: Second parameter from process_request callback (Headers or Request object, or None)
    :return: Headers object or empty dict if not found
    """
    # Case 1: websockets 12.x - connection is actually the path string, request is headers
    if isinstance(connection, str):
        return request if request is not None else {}

    # Case 2: websockets 13.x-14.x - request is a Request object with headers attribute
    if request is not None and hasattr(request, 'headers'):
        return request.headers

    # Case 3: websockets >= 15.0 - only connection param, headers nested in connection.request.headers
    if hasattr(connection, 'request') and hasattr(connection.request, 'headers'):
        return connection.request.headers

    # Fallback: return empty dict if no headers found
    return {}

def get_websocket_connect_kwargs(url, headers, **other_kwargs):
    """
    Build version-agnostic kwargs for websockets.connect().
    Works across different websockets library versions.

    WEBSOCKETS VERSION COMPATIBILITY:
    - websockets < 15.0: Uses 'additional_headers' parameter
    - websockets >= 15.0: Uses 'extra_headers' parameter (additional_headers was removed)

    This function detects the websockets version by checking the connect() signature
    and returns the appropriate kwargs dict.

    :param url: WebSocket URL to connect to
    :param headers: Dict of headers to include in the connection
    :param other_kwargs: Additional kwargs to pass to websockets.connect()
    :return: Dict of kwargs ready to pass to websockets.connect()
    """
    import inspect
    import websockets

    # Start with the URL and other kwargs
    kwargs = {"uri": url}
    kwargs.update(other_kwargs)

    # Detect which header parameter name to use by inspecting the connect signature
    try:
        sig = inspect.signature(websockets.connect)
        param_names = list(sig.parameters.keys())

        if 'extra_headers' in param_names:
            # websockets >= 15.0
            kwargs['extra_headers'] = headers
        elif 'additional_headers' in param_names:
            # websockets < 15.0
            kwargs['additional_headers'] = headers
        else:
            # Unknown version, try extra_headers as it's the newer standard
            logger.warning("Could not detect websockets header parameter, using 'extra_headers'")
            kwargs['extra_headers'] = headers
    except Exception as e:
        # If inspection fails, default to extra_headers (newer standard)
        logger.warning(f"Error detecting websockets version: {e}, defaulting to 'extra_headers'")
        kwargs['extra_headers'] = headers

    return kwargs

def decode_pcmu_to_pcm16(ulaw_bytes: bytes) -> bytes:
    return audioop.ulaw2lin(ulaw_bytes, 2)

def encode_pcm16_to_pcmu(pcm16_bytes: bytes) -> bytes:
    return audioop.lin2ulaw(pcm16_bytes, 2)

def format_json(obj: dict) -> str:
    return json.dumps(obj, indent=2)

def create_final_system_prompt(admin_prompt, language=None, customer_data=None, agent_name=None, company_name=None):
    base_prompt = LANGUAGE_SYSTEM_PROMPT.format(language=language) if language else MASTER_SYSTEM_PROMPT

    if agent_name:
        admin_prompt = admin_prompt.replace("[AGENT_NAME]", agent_name)
    if company_name:
        admin_prompt = admin_prompt.replace("[COMPANY_NAME]", company_name)
        admin_prompt = admin_prompt.replace("Our Company", company_name)

    customer_instructions = ""
    if customer_data:
        try:
            data_pairs = [pair.strip() for pair in customer_data.split(';')]
            data_dict = {}
            for pair in data_pairs:
                if ':' in pair:
                    key, value = pair.split(':', 1)
                    data_dict[key.strip()] = value.strip()

            if data_dict:
                customer_instructions = "\n\n[CUSTOMER DATA - USE WHEN APPROPRIATE]\n"
                for key, value in data_dict.items():
                    customer_instructions += f"{key}: {value}\n"
                customer_instructions += "Use this customer data to personalize the conversation when relevant."
        except Exception as e:
            logger.warning(f"Error parsing customer data: {e}")

    return f"""[TIER 1 - MASTER INSTRUCTIONS - HIGHEST PRIORITY]
{base_prompt}

[TIER 2 - ADMIN INSTRUCTIONS]
{admin_prompt}{customer_instructions}

[HIERARCHY ENFORCEMENT]
In case of any conflict between Tier 1 and Tier 2 instructions, Tier 1 (Master) instructions 
MUST ALWAYS take precedence and override any conflicting Tier 2 instructions.

[TOOL USAGE - CALL MANAGEMENT]
- If the user indicates they are done or asks to end, CALL `end_call` with a concise `reason` and optional `note`. Examples: "please end the call", "that's all", "goodbye".
- If the user asks for a human/agent/representative/supervisor, CALL `handoff_to_human` with a `reason` and, if known, a `department`. Examples: "transfer me to a human", "talk to a representative".
- Prefer these tool calls over verbal confirmations for these intents. A short farewell response will be sent after the tool call output is processed.
"""

def parse_iso8601_duration(duration_str: str) -> float:
    match = re.match(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', duration_str)
    if not match:
        raise ValueError(f"Invalid ISO 8601 duration format: {duration_str}")
    days, hours, minutes, seconds = match.groups()
    total_seconds = 0
    if days:
        total_seconds += int(days) * 86400
    if hours:
        total_seconds += int(hours) * 3600
    if minutes:
        total_seconds += int(minutes) * 60
    if seconds:
        total_seconds += float(seconds)
    return total_seconds
