"""+-----------------+      AetherBus (Redis Streams)      +---------------------------------+
| External Agent  |------------------------------------->| mcp_bridge_gem.py               |
| (e.g., Muse)    |     (INBOX_CHANNEL)                  |  async def main():               |
+-----------------+                                      |    redis = ...                  |
        ^                                                |    subscribe(..., process_env)  |
        |                                                +---------------------------------+
        |                                                               | process_envelope(env, redis)
        |                                                               V
        |                                                +---------------------------------+
        |                                                | if env.content.action ==        |
        |                                                |   "discover_mcp_tools":        |
        |                                                |     handle_discovery_request()  |
        |                                                | elif env.content.action ==      |
        |                                                |   "execute_mcp_tool":          |
        |                                                |     handle_execution_request()  |
        |                                                +---------------------------------+
        |                                                         |                  |
        |                                                         V                  V
        |                                  +--------------------------------+  +-----------------------------------+
        |                                  | handle_discovery_request()     |  | handle_execution_request()        |
        |                                  |  - Parses env.content          |  |  - Parses env.content             |
        |                                  |  - Calls _perform_sdk_discovery|  |  - Calls _perform_mcp_tool_exec |
        |                                  |  - Builds response Envelope    |  |  - Builds response Envelope       |
        |                                  +--------------------------------+  +-----------------------------------+
        |                                                         |                  |
        | (Response Envelope                                      | (Response        | (Response
        |  with manifest or error)                                V                  V  Envelope with
        +----------------------------------------------------Publish Eventual Result to env.reply_to ---+  result or error)
                                                               (OUTBOX_CHANNEL or custom)
"""


import asyncio
from dotenv import load_dotenv
load_dotenv()
# import time # Not used directly in the provided snippet, can be removed if not used elsewhere
import json
import base64
import os
import re
import traceback 
import redis.asyncio as aioredis
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import yaml 
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional

from AG1_AetherBus.bus import subscribe, publish_envelope, REDIS_HOST, REDIS_PORT # Assuming REDIS_HOST, REDIS_PORT are defined in bus.py
from AG1_AetherBus.envelope import Envelope
from mcp.client.sse import sse_client
import aiohttp
import httpx
try:
    from yaml import CLoader as ActualLoader
except ImportError:
    from yaml import Loader as ActualLoader # Fallback to Python implementation

try:
    from mcp.client.websocket import websocket_client 
    import mcp
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import McpError
    from mcp.client.websocket import websocket_client
except ImportError:
    print("[bus_to_mcp_bridge] ERROR: Please install the 'mcp' package (pip install mcp)")
    exit(1)

SMITHERY_API_KEY = os.getenv("SMITHERY_API_KEY", "98879696-3205-4ae2-b2e4-76c22c1ab6ac") # Default API key for the bridge   61b21040-8b26-4030-8e6f-0dc07200dbd8

INBOX_CHANNEL = os.getenv("MCP_BRIDGE_INBOX", "AG1:edge:mcp:main:inbox")
OUTBOX_CHANNEL = os.getenv("MCP_BRIDGE_OUTBOX", "AG1:edge:mcp:main:outbox")

async def _call_smithery_registry_api(
    endpoint_url: str, 
    api_key: str
) -> Optional[Dict]:
    """Helper to call the Smithery Registry API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json"
    }
    print(f'[API KEY] {api_key}')
    # Define more granular timeouts
    connect_timeout = 10.0  # Timeout for establishing a connection
    read_timeout = 20.0     # Timeout for receiving data after connection
    total_timeout = 30.0    # Overall timeout for the request

    try:
        print(f"[BridgeRegistryCall] Attempting to connect to: GET {endpoint_url} with connect_timeout={connect_timeout}s")
        async with httpx.AsyncClient(timeout=httpx.Timeout(total_timeout, connect=connect_timeout, read=read_timeout)) as client:
            print(f"[BridgeRegistryCall] Client created. Sending request to: {endpoint_url}")
            response = await client.get(endpoint_url, headers=headers) # The overall timeout applies here
            print(f"[BridgeRegistryCall] Received response status: {response.status_code} from {endpoint_url}")
            response.raise_for_status() 
            print(f"[BridgeRegistryCall] Successfully fetched and parsed JSON from {endpoint_url}")
            return response.json()
    except httpx.TimeoutException as e: # Catch specific timeout exceptions
        print(f"[BridgeRegistryCall] TIMEOUT calling Smithery Registry {endpoint_url}: {type(e).__name__} - {e}")
    except httpx.RequestError as e: # Catch other httpx request errors (ConnectError, ReadError, etc.)
        print(f"[BridgeRegistryCall] REQUEST ERROR calling Smithery Registry {endpoint_url}: {type(e).__name__} - {e}")
    except httpx.HTTPStatusError as e: # Already caught, but good to have explicitly
        print(f"[BridgeRegistryCall] HTTP STATUS ERROR calling Smithery Registry {endpoint_url}: {e.response.status_code} - {e.response.text}")
    except json.JSONDecodeError as e:
        print(f"[BridgeRegistryCall] JSON DECODE ERROR from Smithery Registry {endpoint_url}: {e}. Response text: {response.text[:200] if 'response' in locals() else 'N/A'}")
    except Exception as e:
        print(f"[BridgeRegistryCall] GENERIC ERROR calling Smithery Registry {endpoint_url}: {type(e).__name__} - {e}")
        # import traceback
        # traceback.print_exc() # For more detailed debugging if needed
    return None

def is_likely_fqn(query: str) -> bool: # Keep this helper
    if not query: return False
    return query.startswith('@') and query.count('/') == 1 and ' ' not in query and len(query) > 3

def make_ws_url(endpoint_input, config_payload, api_key_arg):
    parsed_input_url = urlparse(endpoint_input)
    is_smithery_alias = False

    if parsed_input_url.scheme in ["ws", "wss"]:
        scheme = parsed_input_url.scheme
        netloc = parsed_input_url.netloc
        base_path = parsed_input_url.path.rstrip("/")
    elif parsed_input_url.scheme in ["http", "https"]:
        scheme = "wss" # Upgrade to wss for WebSocket
        netloc = parsed_input_url.netloc
        base_path = parsed_input_url.path.rstrip("/")
        if "server.smithery.ai" in netloc: # It's a Smithery URL
            is_smithery_alias = True # Treat it like an alias for pathing
    else: # Assume it's an alias like "@user/service"
        scheme = "wss"
        netloc = "server.smithery.ai"
        base_path = f"/{endpoint_input.strip('/')}"
        is_smithery_alias = True

    # --- MODIFIED PATH LOGIC for WebSocket ---
    # For Smithery services (aliases or direct Smithery URLs),
    # the WebSocket endpoint is typically at /mcp, not /mcp/call/ws.
    # For other generic WebSocket URLs, original_path.endswith('/mcp') might still be a good heuristic,
    # or they might already include their full WebSocket path.

    original_path_from_input = urlparse(endpoint_input).path.rstrip('/')

    if is_smithery_alias:
        # If base_path already ends with /mcp (e.g., from a full URL like https://.../service/mcp)
        if base_path.endswith('/mcp'):
            mcp_call_path = base_path
        else:
            # For aliases like @user/service or URLs like https://.../service, append /mcp
            mcp_call_path = f"{base_path}/mcp"
    elif original_path_from_input.endswith('/mcp'): # Non-Smithery URL that already specifies /mcp
        mcp_call_path = base_path
    elif original_path_from_input.endswith('/mcp/call/ws'): # Non-Smithery URL that already specifies full path
        mcp_call_path = base_path
    else: # Fallback for generic non-Smithery URLs that don't specify /mcp
        mcp_suffix = "/mcp/call/ws" # Default suffix for generic MCP WS
        if base_path == "/" or not base_path:
            mcp_call_path = mcp_suffix
        else:
            mcp_call_path = f"{base_path}{mcp_suffix}"
    # --- END OF MODIFIED PATH LOGIC ---
    
    query_params = parse_qs(parsed_input_url.query) # Start with existing query params from input
    
    # Add/overwrite 'config' query parameter
    config_to_encode = config_payload if config_payload is not None else {}
    if config_to_encode: # Only add if there's something to encode
        query_params["config"] = [base64.b64encode(json.dumps(config_to_encode).encode()).decode()]
    
    # Add/overwrite 'api_key' query parameter
    if api_key_arg:
        query_params["api_key"] = [api_key_arg]
    # If api_key_arg is not provided, but api_key was in the original endpoint_input's query,
    # it will be preserved from the initial parse_qs.

    final_query_string = urlencode(query_params, doseq=True)
    ws_url_parts = (scheme, netloc, mcp_call_path, '', final_query_string, '')
    final_ws_url = urlunparse(ws_url_parts)
    
    print(f"[make_ws_url] Input Endpoint: '{endpoint_input}'")
    print(f"[make_ws_url] Config Payload (for 'config' param): {config_payload}")
    print(f"[make_ws_url] API Key Arg (for 'api_key' param): '{query_params.get('api_key', [''])[0]}'") # Safer get
    print(f"[make_ws_url] Determined WebSocket Path: '{mcp_call_path}'")
    print(f"[make_ws_url] Generated URL: {final_ws_url}")
    return final_ws_url

def make_mcp_http_service_url(endpoint_input, url_config_payload, api_key_arg):
    parsed_input_url = urlparse(endpoint_input)
    is_alias = False
    is_localhost = "localhost" in parsed_input_url.netloc or "127.0.0.1" in parsed_input_url.netloc

    if parsed_input_url.scheme in ["http", "https"]: 
        # If it's localhost and scheme is http, keep http. Otherwise, default/upgrade to https.
        if is_localhost and parsed_input_url.scheme == "http":
            scheme = "http"
        else:
            scheme = "https" # Default to https for remote or if already https
        netloc = parsed_input_url.netloc
        service_path_base = parsed_input_url.path.rstrip("/")
        # For full URLs, assume /mcp is already there if needed, or it's a non-Smithery endpoint
        # If it's a Smithery URL not ending in /mcp, we might still want to append it.
        if "server.smithery.ai" in netloc and not service_path_base.endswith("/mcp"):
             service_path = f"{service_path_base}/mcp"

        elif (is_localhost or "server.smithery.ai" not in netloc) and not service_path_base.endswith("/mcp"):
            # If your local server expects /mcp, you might need to add it.
            # For now, let's assume the provided path is what it needs, or it's already there.
            # If your local server (e.g. pubmed) needs /mcp, uncomment and adapt:
            # service_path = f"{service_path_base.rstrip('/')}/mcp" 
            service_path = service_path_base # Assume path is as given for non-Smithery

        else:
            service_path = service_path_base
        initial_query_params = parse_qs(parsed_input_url.query)

    else: # Assume it's an alias like "@user/service"
        is_alias = True
        scheme = "https"
        netloc = "server.smithery.ai"
        # For an alias, service_path_base is like /@user/service. We need to append /mcp.
        service_path_base = f"/{endpoint_input.strip('/')}"
        service_path = f"{service_path_base}/mcp" # Standard Smithery MCP path for streamablehttp/websocket
        initial_query_params = {} # Aliases don't have initial query params

    # Prepare query parameters, merging with existing if any
    query_params = initial_query_params
    
    # The url_config_payload (e.g., {"dynamic": ..., "profile": ..., "smitheryApiKey": ...})
    # becomes the 'config' query parameter, base64 encoded.
    if url_config_payload: # Ensure it's not None or empty
        query_params["config"] = [base64.b64encode(json.dumps(url_config_payload).encode()).decode()]
    
    # The api_key_arg is the 'api_key' query parameter.
    if api_key_arg:
        query_params["api_key"] = [api_key_arg]
    elif "api_key" in query_params and not api_key_arg: # If api_key was in original URL and no override
        pass # Keep it
    
    final_query_string = urlencode(query_params, doseq=True)
    
    url_parts = (scheme, netloc, service_path, '', final_query_string, '')
    final_url = urlunparse(url_parts)
    
    print(f"[make_mcp_http_service_url] Input Endpoint: '{endpoint_input}'")
    print(f"[make_mcp_http_service_url] URL Config Payload (for 'config' param): {url_config_payload}")
    print(f"[make_mcp_http_service_url] API Key Arg (for 'api_key' param): '{api_key_arg if api_key_arg else query_params.get('api_key', [None])[0]}'")
    print(f"[make_mcp_http_service_url] Generated Service URL: {final_url}")
    return final_url

def make_json_safe(obj):
    # ... (your existing function)
    if hasattr(obj, "to_dict"):
        return make_json_safe(obj.to_dict())
    elif isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    elif hasattr(obj, "__dict__"): # For simple objects
        # Be cautious with this, might expose too much or circular refs for complex objects
        # Only include non-private, non-callable attributes for safety
        return {k: make_json_safe(v) for k, v in vars(obj).items() if not k.startswith('_') and not callable(v)}
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else: # For other types, convert to string as a fallback
        try:
            # Attempt a direct JSON serialization to catch types like Decimal, datetime
            # This is a bit of a guess; a more robust solution uses custom encoders
            json.dumps(obj) 
            return obj 
        except TypeError:
            return str(obj)

def make_mcp_sse_url(endpoint_input, mcp_service_config_payload, api_key_for_gateway):
    # endpoint_input for local healthcare server will be e.g., "http://localhost:8000"
    # The /mcp/sse path is specific to how healthcare-mcp-public mounts its SSE app.
    
    parsed_input_url = urlparse(endpoint_input)
    scheme = parsed_input_url.scheme or "http" # Default to http for local
    netloc = parsed_input_url.netloc
    base_path = parsed_input_url.path.rstrip("/")

    # Path for healthcare-mcp-public's MCP-over-SSE endpoint
    # This could be made more flexible if other SSE services use different paths
    mcp_via_sse_path = "/mcp/sse" 
    
    if base_path and base_path != "/":
        full_path = f"{base_path}{mcp_via_sse_path}"
    else:
        full_path = mcp_via_sse_path
        
    # Query parameters for the SSE connection URL.
    # For local healthcare-mcp-public, these are likely not needed on the URL itself.
    # If connecting to a Smithery-hosted MCP-over-SSE endpoint, api_key_for_gateway might be used.
    # mcp_service_config_payload is for the MCP session, not typically the SSE URL's query.
    query_params = {} 
    if "server.smithery.ai" in netloc and api_key_for_gateway: # Example for Smithery
         query_params["api_key"] = [api_key_for_gateway]
    
    final_query_string = urlencode(query_params, doseq=True)
    url_parts = (scheme, netloc, full_path, '', final_query_string, '')
    final_url = urlunparse(url_parts)

    print(f"[make_mcp_sse_url] Input Endpoint: '{endpoint_input}'")
    # mcp_service_config_payload is for the MCP session's 'params' not usually the URL here
    print(f"[make_mcp_sse_url] MCP Service Config (for session): {mcp_service_config_payload}")
    print(f"[make_mcp_sse_url] API Key for Gateway (if any): '{api_key_for_gateway}'")
    print(f"[make_mcp_sse_url] Generated URL for MCP-over-SSE: {final_url}")
    return final_url

async def call_tool_via_rest_post(base_http_url: str, tool_name: str, tool_args: dict, 
                                  mcp_service_config: dict, 
                                  api_key_for_gateway: str):
                                  
    post_target_url = f"{base_http_url.rstrip('/')}/mcp/call-tool"
    request_payload = {
        "name": tool_name,
        "arguments": tool_args
    }

    print(f"[ccg to: {post_target_url}")
    print(f"[call_tool_via_rest_post] Payload: {json.dumps(request_payload)}")

    # For local calls, we don't typically need to pass the Smithery API key
    # or complex config in headers unless the local server specifically requires it.
    # The healthcare server's /mcp/call-tool doesn't seem to require special headers
    # beyond Content-Type: application/json, which aiohttp sets by default for json=payload.
    headers = {}

    timeout_settings = aiohttp.ClientTimeout(total=60, connect=10)

    async with aiohttp.ClientSession(timeout=timeout_settings) as http_session:
        async with http_session.post(post_target_url, json=request_payload, headers=headers) as response:
            print(f"[call_tool_via_rest_post] Response Status: {response.status}")
            response_data = await response.json() # Assuming it always returns JSON
            print(f"[call_tool_via_rest_post] Response Data: {response_data}")
            
            # Check for application-level success within the JSON response
            if response.status == 200 and response_data.get("status") == "success":
                return response_data # Or response_data.get("results") or whatever the actual data is
            elif response.status == 200 and "error_message" in response_data: # Handle app-level errors
                 raise Exception(f"Tool call failed (app error): {response_data.get('error_message')}")
            else:
                response.raise_for_status() # Raise an exception for HTTP 4xx/5xx errors
                return response_data # Should not be reached if raise_for_status() fires

async def call_mcp_via_sse(endpoint_str, mcp_service_config, tool_name, tool_args, api_key_for_gateway):
    # endpoint_str: e.g., "http://localhost:8000"
    # mcp_service_config: dict from Envelope.content.config, for the MCP Session
    # tool_name, tool_args: for session.call_tool
    # api_key_for_gateway: for the URL if connecting to Smithery gateway

    service_url_for_sse = make_mcp_sse_url(endpoint_str, mcp_service_config, api_key_for_gateway)
    
    # Headers for the SSE connection (e.g., if custom auth was needed here)
    # For healthcare-mcp-public locally, likely no special headers needed.
    # aiohttp_sse_client (which mcp.client.sse likely uses or emulates) sets "Accept: text/event-stream"
    custom_headers_for_sse = None 

    # Timeouts for sse_client itself
    connection_timeout = 30  # Corresponds roughly to 'timeout' in other clients
    event_read_timeout = 300 # Corresponds to 'sse_read_timeout'

    try:
        # sse_client establishes the HTTP connection and handles SSE protocol.
        async with sse_client(
            service_url_for_sse, 
            headers=custom_headers_for_sse,
            timeout=connection_timeout,
            sse_read_timeout=event_read_timeout
        ) as (read_stream, write_stream):
            # `read_stream` and `write_stream` are then used by `mcp.ClientSession`
            # Now, pass mcp_service_config to ClientSession if it's needed for the MCP protocol layer
            async with mcp.ClientSession(
                read_stream, 
                write_stream, 
                config=mcp_service_config # <--- PASS MCP SESSION CONFIG HERE
            ) as session:
                await session.initialize() # MCP handshake
                print(f"[call_mcp_via_sse] MCP session initialized over SSE for {service_url_for_sse}")
                result = await session.call_tool(tool_name, tool_args)
                print(f"[call_mcp_via_sse] Tool '{tool_name}' called successfully via SSE.")
                return result
    except aiohttp.ClientResponseError as e_http: # Catch specific aiohttp errors if sse_client raises them
        print(f"[call_mcp_via_sse] HTTP ClientResponseError: Status {e_http.status}, Message: {e_http.message}, URL: {service_url_for_sse}")
        raise # Re-raise to be caught by process_envelope's general Exception handler
    except Exception as e:
        print(f"[call_mcp_via_sse] Error during SSE MCP call to {service_url_for_sse}: {type(e).__name__} - {e}")
        raise # Re-raise

async def call_mcp(endpoint_str, mcp_config_payload, tool_name, tool_args, final_api_key):
    # endpoint_str can be an alias or full URL.
    # mcp_config_payload is the dict for the 'config' query param.
    # final_api_key is the API key to be used.
    ws_url = make_ws_url(endpoint_str, mcp_config_payload, final_api_key)
    async with websocket_client(ws_url) as streams:
        async with mcp.ClientSession(*streams) as session:
            await session.initialize() # Performs MCP hello handshake
            result = await session.call_tool(tool_name, tool_args)
            return result

# Modify call_mcp
async def call_mcp_via_http(endpoint_str, mcp_url_config_payload, tool_name, tool_args, final_api_key):
    # mcp_url_config_payload is for the URL's 'config' query parameter.
    # It includes {"dynamic": False, "profile": "...", "smitheryApiKey": "..."}
    service_url = make_mcp_http_service_url(endpoint_str, mcp_url_config_payload, final_api_key)
    
    print(f"[call_mcp_via_http] Connecting to service_url: {service_url}")
    print(f"[call_mcp_via_http] Connecting to service_url: {mcp_url_config_payload}")
    print(f"[call_mcp_via_http] Connecting to service_url: {final_api_key}")
    
    
    async with streamablehttp_client(service_url) as (read_stream, write_stream, _): 
        # DO NOT pass a 'config' kwarg to ClientSession here.
        # The necessary Smithery 'profile' config is already in the service_url.
        async with mcp.ClientSession(read_stream, write_stream) as session:
            await session.initialize() # Performs MCP hello handshake
            print(f"[call_mcp_via_http] MCP session initialized. Calling tool: {tool_name}")
            result = await session.call_tool(tool_name, tool_args)
            print(f"[call_mcp_via_http] Tool result: {str(result)[:90]}")
            return result

# -- - build_redis_url (ensure it's defined or imported correctly) ---
def build_redis_url():
    user = os.getenv("REDIS_USERNAME")
    pwd = os.getenv("REDIS_PASSWORD")
    host = os.getenv("REDIS_HOST", "localhost") # Use REDIS_HOST from .env or default
    port = int(os.getenv("REDIS_PORT", 6379)) # Use REDIS_PORT from .env or default
    if user and pwd:
        return f"redis://{user}:{pwd}@{host}:{port}"
    elif pwd: # Support for password-only Redis (e.g. some cloud providers)
        return f"redis://:{pwd}@{host}:{port}"
    else:
        return f"redis://{host}:{port}"
# --- end of build_redis_url ---


#------------------------------------------------------------------------------------------------
# This will be the core SDK logic for discovery
#------------------------------------------------------------------------------------------------

def parse_env_file_for_potential_keys(env_content: str) -> dict | None:
    """
    VERY HEURISTIC: Tries to extract potential API keys from .env file content.
    Looks for lines like KEY_NAME=value and assumes anything with "KEY" in its name
    might be relevant. This is highly unreliable for determining structured URL config.
    Returns a schema-like dict: {"guessed_url_config_key": {"description": "From .env", "x-suggested-client-config-key": "ENV_VAR_NAME"}}
    """
    if not env_content:
        return None
    
    potential_schema = {}
    lines = env_content.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'): # Skip empty lines and comments
            continue
        
        if '=' not in line: # Skip lines without an assignment
            continue

        env_var_name, *env_var_value_parts = line.split('=', 1)
        env_var_name = env_var_name.strip()
        # env_var_value = env_var_value_parts[0].strip() if env_var_value_parts else "" # Value not used for schema

        # Heuristic: if "KEY" is in the variable name, consider it.
        if "KEY" in env_var_name.upper():
            # Super naive guess for the URL config key name:
            # Attempt to convert SNAKE_CASE_UPPER to camelCase (e.g., MEM0_API_KEY -> mem0ApiKey)
            parts = env_var_name.lower().split('_')
            guessed_url_config_key = parts[0] + "".join(p.capitalize() for p in parts[1:])
            
            potential_schema[guessed_url_config_key] = {
                "description": f"Potentially from .env file variable: {env_var_name}",
                "x-suggested-client-config-key": env_var_name # Suggest client looks for the exact .env var name
            }
    
    if potential_schema:
        print(f"[BridgeParseEnv] Heuristically extracted from .env-like content: {json.dumps(potential_schema)}")
        return potential_schema
    return None

# Helper to extract GitHub repo URL from Smithery server page HTML
def extract_github_url_from_html(html_content: str, server_qname: str | None = None) -> str | None:
    if not html_content:
        print("[extract_github_url] HTML content is empty.")
        return None
    soup = BeautifulSoup(html_content, 'html.parser')
    print(f"[extract_github_url] Parsing HTML for server: {server_qname}")

    # Primary Heuristic: Find an <a> tag with href starting with "https://github.com/"
    # and link text exactly "GitHub" (case-insensitive).
    for a_tag in soup.find_all('a', href=re.compile(r"^https://github.com/")):
        href = a_tag.get('href', '').split("#")[0].rstrip('/') # Clean URL
        text = a_tag.get_text(strip=True)
        
        print(f"[extract_github_url] Checking link: Text='{text}', Href='{href}'") # Debug print

        if text.lower() == "github":
            # Further check: ensure it's a base repo URL (owner/repo)
            path_segments = urlparse(href).path.strip('/').split('/')
            if len(path_segments) == 2: # owner/repo
                print(f"[extract_github_url] Found by 'GitHub' text and repo structure: {href}")
                return href
            else:
                print(f"[extract_github_url] Link with 'GitHub' text is not a base repo URL: {href}")
        
    # Fallback: If the above doesn't work, try to find a link whose text is the repo name itself
    # This is more complex and might be needed if the link text isn't "GitHub"
    if server_qname:
        qname_parts = server_qname.split('/')
        if len(qname_parts) == 2:
            owner_from_qname = qname_parts[0][1:].lower() # remove @
            repo_name_from_qname = qname_parts[1].lower()
            for a_tag in soup.find_all('a', href=re.compile(r"^https://github.com/")):
                href = a_tag.get('href', '').split("#")[0].rstrip('/')
                text = a_tag.get_text(strip=True).lower()
                print(f"[extract_github_url] Fallback check: Text='{text}', Href='{href}'")
                
                path_segments = urlparse(href).path.strip('/').split('/')
                if len(path_segments) == 2:
                    href_owner = path_segments[0].lower()
                    href_repo = path_segments[1].lower()
                    # Check if link text matches owner/repo or just repo from qname
                    if text == f"{owner_from_qname}/{repo_name_from_qname}" or text == repo_name_from_qname:
                        if href_owner == owner_from_qname and href_repo == repo_name_from_qname:
                             print(f"[extract_github_url] Found by matching link text to qname parts: {href}")
                             return href
                    # Check if href path matches owner/repo from qname, even if text is different
                    if href_owner == owner_from_qname and href_repo == repo_name_from_qname:
                        print(f"[extract_github_url] Found by matching href path to qname parts: {href}")
                        return href


    print(f"[extract_github_url] No definitive GitHub repo URL found with primary heuristics for {server_qname}.")
    # Keeping a very simple regex as a last resort if the link isn't standard
    match = re.search(r'href=["\'](https://github.com/[^/"\']+/[^/"\']+)["\']', html_content, re.IGNORECASE)
    if match:
        url = match.group(1).split("#")[0].rstrip('/')
        # Final check to ensure it's a base repo
        path_segments = urlparse(url).path.strip('/').split('/')
        if len(path_segments) == 2:
            print(f"[extract_github_url] Found via broad href regex: {url}")
            return url

    print(f"[extract_github_url] All heuristics failed for {server_qname}.")
    return None

def _parse_smithery_yaml_for_config_schema(yaml_content: str) -> dict | None:
    """
    Parses YAML content (hopefully from a smithery.yaml file)
    and extracts the startCommand.configSchema.
    """
    if not yaml_content:
        print("[_parse_smithery_yaml] YAML content is empty.")
        return None
    try:
        #data = yaml.load(yaml_content, Loader=Loader) # Using safe_load is better if available and you don't need custom tags
        data = yaml.load(yaml_content, Loader=ActualLoader)
        # data = yaml.safe_load(yaml_content) # Generally preferred
        if isinstance(data, dict):
            start_command = data.get('startCommand')
            if isinstance(start_command, dict):
                config_schema = start_command.get('configSchema')
                if isinstance(config_schema, dict):
                    print("[_parse_smithery_yaml] Successfully extracted configSchema.")
                    return config_schema
                else:
                    print("[_parse_smithery_yaml] 'configSchema' not found or not a dict in 'startCommand'.")
            else:
                print("[_parse_smithery_yaml] 'startCommand' not found or not a dict in YAML.")
        else:
            print("[_parse_smithery_yaml] Parsed YAML is not a dictionary.")
        return None
    except yaml.YAMLError as e:
        print(f"[_parse_smithery_yaml] Error parsing YAML: {e}")
        return None
    except Exception as e: # Catch any other unexpected errors during parsing/access
        print(f"[_parse_smithery_yaml] Unexpected error processing YAML data: {e}")
        return None

async def _fetch_raw_github_file(
    repo_url: str, # This should be the base GitHub repo URL, e.g., https://github.com/user/repo
    target_filename: str, # e.g., "smithery.yaml"
    # Config needed to call the @smithery-ai/fetch service via the gateway:
    fetch_service_connection_config: dict, # Contains Smithery profile for @smithery-ai/fetch
    gateway_api_key_for_fetch_service: str # Smithery Gateway API key
) -> str | None:
    if not repo_url or not target_filename:
        return None

    # Construct raw.githubusercontent.com URL
    parsed_repo_url = urlparse(repo_url)
    path_parts = [part for part in parsed_repo_url.path.split('/') if part]
    if len(path_parts) < 2:
        print(f"[_fetch_raw_github_file] Could not parse owner/repo from {repo_url}")
        return None
    owner, repo = path_parts[0], path_parts[1]

    raw_file_content = None
    for branch in ["main", "master"]: # Try common default branches
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{target_filename}"
        print(f"[_fetch_raw_github_file] Attempting to fetch: {raw_url}")
        
        fetch_result = await _perform_mcp_tool_execution(
            protocol="streamablehttp",
            endpoint_id="@smithery-ai/fetch",
            tool_name="fetch",
            tool_args={"url": raw_url, "raw": True}, # Assuming "raw": True is still desired
            mcp_url_config=fetch_service_connection_config, # Config for @smithery-ai/fetch
            api_key=gateway_api_key_for_fetch_service
        )

        if fetch_result.get("status") == "success":
            result_content = fetch_result.get("result", {}).get("content", [])
            if result_content and result_content[0].get("type") == "text":
                # Check if fetch tool indicates an error (e.g., 404 for the raw file)
                # The @smithery-ai/fetch tool might return the error page content as text.
                # A simple check:
                text_data = result_content[0]["text"]
                if "404: Not Found" in text_data and raw_url in text_data: # Basic check for 404 page
                    print(f"[_fetch_raw_github_file] Fetch returned 404 for {raw_url}")
                    continue # Try next branch
                raw_file_content = text_data
                break # Successfully fetched
        else:
            print(f"[_fetch_raw_github_file] MCP call to @smithery-ai/fetch failed for {raw_url}: {fetch_result.get('error')}")
    
    if not raw_file_content:
        print(f"[_fetch_raw_github_file] Failed to fetch {target_filename} from {repo_url} on any common branch.")
    return raw_file_content


async def oldhandle_discovery_request(request_content: dict, redis_client: aioredis.Redis, reply_to_channel: str, original_envelope: Envelope):
    print(f"[BridgeDiscoveryHandler] Received AetherBus discovery request.")
    discovery_target_info = request_content.get("discovery_target", {})
    bridge_smithery_api_key = request_content.get("smithery_gateway_api_key") or SMITHERY_API_KEY
    # smithery_profile_id_for_helpers = request_content.get("smithery_profile_id") # Not directly used if using Registry API for manifests
    # agent_owned_service_api_keys = request_content.get("service_api_keys", {}) # Not directly used by bridge if registry provides all

    # This will be the actual content sent back to the agent
    response_payload_for_agent: Dict[str, Any] = {"status": "error", "error": "Discovery process did not complete as expected."}
    
    server_manifests_from_registry = [] # List to hold manifests we get from Smithery Registry
    search_query = discovery_target_info.get("search_query", "")

    if is_likely_fqn(search_query):
        registry_url = f"https://registry.smithery.ai/servers/{search_query}" # Use FQN directly
        print(f"[BridgeDiscoveryHandler] Direct FQN. Calling Registry API: {registry_url}")
        manifest = await _call_smithery_registry_api(registry_url, bridge_smithery_api_key)
        if manifest:
            server_manifests_from_registry.append(manifest)
            # If we got a manifest, the operation was a success at this stage
            response_payload_for_agent = {"status": "success_fqn_manifest_retrieved"} # Temporary status
        else:
            response_payload_for_agent = {"status": "error", "error": f"Failed to fetch manifest for FQN '{search_query}' from Smithery Registry."}
    
    elif discovery_target_info.get("type") == "smithery_toolbox_search":
        from urllib.parse import quote
        encoded_query = quote(search_query)
        search_n_results = discovery_target_info.get("n", 5)
        registry_search_url = f"https://registry.smithery.ai/servers?q={encoded_query}&page=1&pageSize={search_n_results}"
        print(f"[BridgeDiscoveryHandler] Toolbox Search. Calling Registry API: {registry_search_url}")
        search_result_data = await _call_smithery_registry_api(registry_search_url, bridge_smithery_api_key)
        print('--->>')
        if search_result_data:
            print(f"[BridgeDiscoveryHandler] RAW search_result_data from registry for '{search_query}': {json.dumps(search_result_data, indent=2)}")
            # Adjust based on actual structure of search_result_data
            if isinstance(search_result_data, list):
                server_manifests_from_registry = search_result_data
            elif isinstance(search_result_data, dict) and "data" in search_result_data and isinstance(search_result_data["data"], list):
                server_manifests_from_registry = search_result_data["data"]
            elif isinstance(search_result_data, dict) and "servers" in search_result_data and isinstance(search_result_data["servers"], list): # Another common pattern
                server_manifests_from_registry = search_result_data["servers"]
            else:
                print(f"[BridgeDiscoveryHandler] Registry search for '{search_query}' returned unexpected format: {str(search_result_data)[:200]}")
                response_payload_for_agent = {"status": "error", "error": "Registry search returned unexpected data format."}

            if server_manifests_from_registry:
                response_payload_for_agent = {"status": "success_search_manifests_retrieved"} # Temporary status
            elif response_payload_for_agent.get("status") != "error": # Search was successful but returned no items
                 response_payload_for_agent = {"status": "success_search_empty", "tool_blueprints": [], "notes": f"No servers found via Smithery Registry for query '{search_query}'."}
        else: # API call for search failed
            response_payload_for_agent = {"status": "error", "error": f"Failed to search Smithery Registry for '{search_query}'."}
    else:
        response_payload_for_agent = {"status": "error", "error": f"Unsupported discovery type: {discovery_target_info.get('type')}"}

    # --- Assemble Tool Blueprints if manifests were retrieved ---
    if response_payload_for_agent["status"].startswith("success_"): # Check for our temporary success statuses
        if not server_manifests_from_registry:
             # This case handles if search was successful but returned an empty list of servers
            response_payload_for_agent = {"status": "success", "tool_blueprints": [], "notes": response_payload_for_agent.get("notes", "No servers found or processed.")}
        else:
            enriched_tools_blueprints = []
            for registry_manifest in server_manifests_from_registry:
                qname = registry_manifest.get("qualifiedName")
                if not qname: continue

                connection_config_schema = {} # Default to empty
                connections = registry_manifest.get("connections", [])
                if connections and isinstance(connections, list) and len(connections) > 0:
                    http_connection = next((c for c in connections if c.get("type") == "http"), connections[0])
                    if http_connection: # Ensure http_connection is not None
                        connection_config_schema = http_connection.get("configSchema", {}) # Default to {} if missing
                
                connection_config_notes = f"Schema from Smithery Registry for {qname}."
                if not connection_config_schema: # Check if it's still empty
                    connection_config_notes += " (No configSchema explicitly provided by registry)."

                for tool_def in registry_manifest.get("tools", []):
                    enriched_tools_blueprints.append({
                        "server_qualified_name": qname,
                        "server_display_name": registry_manifest.get("displayName", qname),
                        "server_homepage": registry_manifest.get("homepage"),
                        "server_github_repo_url": registry_manifest.get("sourceUrl"), # Smithery Registry uses 'sourceUrl' for GitHub
                        "tool_name": tool_def.get("name"),
                        "tool_description": tool_def.get("description"),
                        "tool_input_schema": tool_def.get("inputSchema"),
                        "connection_url_config_schema": connection_config_schema,
                        "connection_config_notes": connection_config_notes,
                        "protocol_preference": "streamablehttp" 
                    })
            response_payload_for_agent = {"status": "success", "tool_blueprints": enriched_tools_blueprints}
            if not enriched_tools_blueprints: # Manifests were there, but no tools in them
                 response_payload_for_agent["notes"] = "Servers found but they listed no tools."


    # --- Publish Response to Agent ---
    final_envelope_type = "mcp_discovery_result" if response_payload_for_agent.get("status") == "success" else "error"
    response_env = Envelope(
        content=response_payload_for_agent,
        role="bridge_service", agent_name="mcp_bridge",
        session_code=original_envelope.session_code, correlation_id=original_envelope.correlation_id,
        envelope_type=final_envelope_type, 
        meta={"source_envelope_id": original_envelope.envelope_id}
    )
    print(f"[BridgeDiscoveryHandler] Publishing final response: {str(response_payload_for_agent)[:200]}...")
    await publish_envelope(redis_client, reply_to_channel, response_env)

async def handle_discovery_request(request_content: dict, redis_client: aioredis.Redis, reply_to_channel: str, original_envelope: Envelope):
    print(f"[BridgeDiscoveryHandler] Received AetherBus discovery request.")
    discovery_target_info = request_content.get("discovery_target", {})
    bridge_smithery_api_key = request_content.get("smithery_gateway_api_key") or SMITHERY_API_KEY
    
    response_payload_for_agent: Dict[str, Any] = {"status": "error", "error": "Discovery process did not initialize correctly."}
    
    # This list will hold FULL manifests obtained from the registry's FQN endpoint
    full_server_manifests_to_process = [] 
    search_query = discovery_target_info.get("search_query", "")

    if is_likely_fqn(search_query):
        # Direct FQN lookup
        registry_fqn_url = f"https://registry.smithery.ai/servers/{search_query}" # search_query includes @
        print(f"[BridgeDiscoveryHandler] Direct FQN. Calling Registry API: {registry_fqn_url}")
        manifest = await _call_smithery_registry_api(registry_fqn_url, bridge_smithery_api_key)
        if manifest:
            full_server_manifests_to_process.append(manifest)
            response_payload_for_agent = {"status": "success_fqn_processed"} # Temp status
        else:
            response_payload_for_agent = {"status": "error", "error": f"Failed to fetch full manifest for FQN '{search_query}' from Smithery Registry."}
    
    elif discovery_target_info.get("type") == "smithery_toolbox_search":
        from urllib.parse import quote
        encoded_query = quote(search_query)
        search_n_results = discovery_target_info.get("n", 5) # This is pageSize for the search
        registry_search_url = f"https://registry.smithery.ai/servers?q={encoded_query}&page=1&pageSize={search_n_results}"
        print(f"[BridgeDiscoveryHandler] General Search. Calling Registry API: {registry_search_url}")
        search_result_data = await _call_smithery_registry_api(registry_search_url, bridge_smithery_api_key)

        if search_result_data:
            summary_manifests = []
            if isinstance(search_result_data, dict) and "servers" in search_result_data and isinstance(search_result_data["servers"], list):
                summary_manifests = search_result_data["servers"]
            # Add other parsing for search_result_data if necessary (e.g. if it's a direct list)
            else:
                 print(f"[BridgeDiscoveryHandler] Registry search for '{search_query}' returned unexpected top-level format: {str(search_result_data)[:200]}")
                 response_payload_for_agent = {"status": "error", "error": "Registry search returned unexpected data format."}

            if summary_manifests:
                print(f"[BridgeDiscoveryHandler] Found {len(summary_manifests)} server summaries for '{search_query}'. Fetching full manifests...")
                fetch_tasks = []
                for summary_manifest in summary_manifests:
                    fqn = summary_manifest.get("qualifiedName")
                    if fqn:
                        registry_fqn_url = f"https://registry.smithery.ai/servers/{fqn}"
                        fetch_tasks.append(_call_smithery_registry_api(registry_fqn_url, bridge_smithery_api_key))
                
                # Fetch full manifests concurrently
                list_of_full_manifests_or_none = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                
                for item in list_of_full_manifests_or_none:
                    if isinstance(item, dict): # Successfully fetched full manifest
                        full_server_manifests_to_process.append(item)
                    elif isinstance(item, Exception):
                        print(f"[BridgeDiscoveryHandler] Error fetching a full manifest during search: {item}")
                    else: # Got None or unexpected type
                        print(f"[BridgeDiscoveryHandler] Failed to fetch a full manifest during search (got None or unexpected type).")
                
                if full_server_manifests_to_process:
                    response_payload_for_agent = {"status": "success_search_processed"} # Temp status
                else: # No full manifests could be fetched from the search results
                    response_payload_for_agent = {"status": "error", "error": f"Found server summaries for '{search_query}', but failed to fetch any full manifests."}

            elif response_payload_for_agent.get("status") != "error": # Search API call was ok, but no server summaries found
                 response_payload_for_agent = {"status": "success_search_empty", "notes": f"No servers found via Smithery Registry for query '{search_query}'."}
        else: # API call for search itself failed
            response_payload_for_agent = {"status": "error", "error": f"Failed to search Smithery Registry for '{search_query}'."}
    else:
        response_payload_for_agent = {"status": "error", "error": f"Unsupported discovery type."}

    # --- Assemble Tool Blueprints from the FULL server manifests ---
    if response_payload_for_agent["status"].startswith("success_"): # Our temp success markers
        enriched_tools_blueprints = []
        if not full_server_manifests_to_process:
            # This handles "success_search_empty" or if FQN fetch yielded manifest but it was then filtered out (unlikely here)
            response_payload_for_agent = {"status": "success", "tool_blueprints": [], "notes": response_payload_for_agent.get("notes", "No valid server manifests to process.")}
        else:
            for full_registry_manifest in full_server_manifests_to_process:
                qname = full_registry_manifest.get("qualifiedName")
                if not qname: continue

                connection_config_schema = {} 
                connections = full_registry_manifest.get("connections", [])
                if connections and isinstance(connections, list) and len(connections) > 0:
                    http_connection = next((c for c in connections if c.get("type") == "http"), connections[0])
                    if http_connection:
                        connection_config_schema = http_connection.get("configSchema", {})
                
                connection_config_notes = f"Schema from Smithery Registry for {qname}."
                if not connection_config_schema:
                    connection_config_notes += " (No configSchema explicitly provided by registry)."

                # The 'tools' array should be in full_registry_manifest now
                for tool_def in full_registry_manifest.get("tools", []):
                    enriched_tools_blueprints.append({
                        "server_qualified_name": qname,
                        "server_display_name": full_registry_manifest.get("displayName", qname),
                        "server_homepage": full_registry_manifest.get("homepage"),
                        "server_github_repo_url": full_registry_manifest.get("sourceUrl"), 
                        "tool_name": tool_def.get("name"),
                        "tool_description": tool_def.get("description"),
                        "tool_input_schema": tool_def.get("inputSchema"),
                        "connection_url_config_schema": connection_config_schema,
                        "connection_config_notes": connection_config_notes,
                        "protocol_preference": "streamablehttp" 
                    })
            
            response_payload_for_agent = {"status": "success", "tool_blueprints": enriched_tools_blueprints}
            if not enriched_tools_blueprints and full_server_manifests_to_process:
                 response_payload_for_agent["notes"] = "Servers found by registry but they listed no tools, or tools could not be processed from full manifests."
    
    # else: response_payload_for_agent already contains an error dict

    # --- Publish Response to Agent ---
    final_envelope_type = "mcp_discovery_result" if response_payload_for_agent.get("status") == "success" else "error"
    response_env = Envelope(
        content=response_payload_for_agent,
        role="bridge_service", agent_name="mcp_bridge",
        session_code=original_envelope.session_code, correlation_id=original_envelope.correlation_id,
        envelope_type=final_envelope_type, 
        meta={"source_envelope_id": original_envelope.envelope_id}
    )
    print(f"[BridgeDiscoveryHandler] Publishing final response: {str(response_payload_for_agent)[:200]}...")
    await publish_envelope(redis_client, reply_to_channel, response_env)

async def handle_execution_request(request_content: dict, redis_client: aioredis.Redis, reply_to_channel: str, original_envelope: Envelope):
    print(f"[BridgeExecutionHandler] Received AetherBus execution request: {str(request_content)[:500]}")
    
    protocol = request_content.get("protocol")
    endpoint_id = request_content.get("endpoint_id") # This is the target server's QName or URL
    tool_name = request_content.get("tool_name")
    tool_args = request_content.get("tool_args", {})
    # This is the config for the TARGET service's URL (e.g., Etherscan/Twitter keys + Smithery profile)
    mcp_url_config_for_target = request_content.get("mcp_url_config", {}) 
    # This is the Smithery Gateway API key
    api_key_for_gateway = request_content.get("api_key_for_gateway") or SMITHERY_API_KEY

    error_response_content = None

    if not all([protocol, endpoint_id, tool_name, api_key_for_gateway is not None]): # api_key can be empty string for local
        error_response_content = {"status": "error", "error": "Missing required parameters for execution (protocol, endpoint_id, tool_name, or api_key_for_gateway)"}
    
    if error_response_content:
        exec_result_payload = error_response_content
    else:
        exec_result_payload = await _perform_mcp_tool_execution(
            protocol, endpoint_id, tool_name, tool_args, mcp_url_config_for_target, api_key_for_gateway
        )

    response_env = Envelope( # Construct response envelope
        content=exec_result_payload, # This is already {"status": ..., "result/error": ...}
        role="bridge_service", agent_name="mcp_bridge",
        session_code=original_envelope.session_code, correlation_id=original_envelope.correlation_id,
        envelope_type="mcp_execution_result" if exec_result_payload.get("status") == "success" else "error",
        meta={"source_envelope_id": original_envelope.envelope_id}
    )
    await publish_envelope(redis_client, reply_to_channel, response_env)


async def process_envelope(env: Envelope, redis):
    content = env.content or {}
    action = content.get("action")
    reply_to = env.reply_to or OUTBOX_CHANNEL # Ensure reply_to is always defined

    print(f"[process_envelope] Action: '{action}', From: {env.agent_name}, ID: {env.envelope_id}")

    try:
        if action == "discover_mcp_tools":
            await handle_discovery_request(content, redis, reply_to, env)
            print('process request out')
        elif action == "execute_mcp_tool":
            await handle_execution_request(content, redis, reply_to, env)
        elif action is None and content.get("tool") and content.get("endpoint"):
            # Fallback for old-style execution requests that don't have "action"
            print(f"[process_envelope] No action specified, defaulting to 'execute_mcp_tool' due to presence of 'tool' and 'endpoint'.")
            # We need to map the old fields to the new expected fields for handle_execution_request
            execution_content = {
                "protocol": content.get("protocol", "streamablehttp"), # Default if not specified
                "endpoint_id": content.get("endpoint"),
                "tool_name": content.get("tool"),
                "tool_args": content.get("args", {}),
                "mcp_url_config": content.get("config", {}), # Old 'config' becomes 'mcp_url_config'
                "api_key_for_gateway": content.get("api_key", SMITHERY_API_KEY)
            }
            await handle_execution_request(execution_content, redis, reply_to, env)
        else:
            error_msg = f"Unknown or underspecified action: '{action}'"
            print(f"[process_envelope] ERROR: {error_msg} in envelope {env.envelope_id}")
            error_env = Envelope(
                content={"error": error_msg, "original_content": content}, 
                envelope_type="error", agent_name="mcp_bridge",
                session_code=env.session_code, correlation_id=env.correlation_id,
                meta={"source_envelope_id": env.envelope_id}
            )
            await publish_envelope(redis, reply_to, error_env)
    except Exception as e:
        tb_str = traceback.format_exc()
        error_msg = f"Unhandled error in process_envelope for action '{action}': {type(e).__name__} - {e}"
        print(f"[process_envelope] FATAL ERROR: {error_msg}\n{tb_str}")
        error_env = Envelope(
            content={"error": error_msg, "details": tb_str, "original_content": content}, 
            envelope_type="error", agent_name="mcp_bridge",
            session_code=env.session_code, correlation_id=env.correlation_id,
            meta={"source_envelope_id": env.envelope_id}
        )
        await publish_envelope(redis, reply_to, error_env)


async def _perform_sdk_discovery(
    target_mcp_url_base: str, 
    search_tool_name: str,
    search_args: dict,
    config_for_url_param: dict,
    api_key_for_url_param: str):

    manifest_data = None
    error_message = None

    from urllib.parse import urlencode, urlparse, urlunparse, parse_qs
    import base64 
    import json 

    if not target_mcp_url_base.endswith('/mcp'):
        # The streamablehttp_client example in the SDK README passes the full URL including /mcp
        # So, we should ensure our base + /mcp is what we use.
        actual_connection_url_base = f"{target_mcp_url_base.rstrip('/')}/mcp"
    else:
        actual_connection_url_base = target_mcp_url_base
    
    # Smithery example for streamablehttp_client uses http/https, not wss, for the base URL.
    # Let's ensure we use https for the scheme if it's not already specified.
    parsed_target_url = urlparse(actual_connection_url_base)
    if parsed_target_url.scheme not in ['http', 'https']:
        actual_connection_url_base = actual_connection_url_base.replace(f"{parsed_target_url.scheme}://", "https://", 1)
        if not actual_connection_url_base.startswith("https://"): # if no scheme was there before
             actual_connection_url_base = f"https://{actual_connection_url_base}"


    parsed_url = urlparse(actual_connection_url_base) # Re-parse after scheme correction
    query_params = parse_qs(parsed_url.query)

    if api_key_for_url_param:
        query_params["api_key"] = [api_key_for_url_param]
    if config_for_url_param:
        query_params["config"] = [base64.b64encode(json.dumps(config_for_url_param).encode()).decode()]
    
    actual_connection_url = urlunparse(parsed_url._replace(scheme='https', query=urlencode(query_params, doseq=True))) # Force https

    print(f"[_perform_sdk_discovery] Attempting StreamableHTTP connection to: {actual_connection_url}")
    print(f"[_perform_sdk_discovery] Will call tool '{search_tool_name}' with args: {search_args}")
    #print(f"[_perform_sdk_discovery] Config for mcp.ClientSession: {session_config_for_mcp_clientsession}")

    try:
        # Use streamablehttp_client
        # It yields: read_stream, write_stream, get_session_id_callback
        async with streamablehttp_client(actual_connection_url) as (read_s, write_s, _): 
            # Pass the unpacked streams directly to ClientSession
            # REMOVE the config=session_config_for_mcp_clientsession argument
            async with mcp.ClientSession(read_s, write_s) as session: # <--- CORRECTED HERE
                await session.initialize()
                print(f"[_perform_sdk_discovery] MCP session initialized. Calling tool '{search_tool_name}'...")
                manifest_data = await session.call_tool(search_tool_name, search_args)
                print(f"[_perform_sdk_discovery] Received manifest data: {str(manifest_data)[:500]}...")

    except ImportError:
        error_message = "[_perform_sdk_discovery] ERROR: 'mcp' package not found or streamablehttp_client missing."
        print(error_message)
    except httpx.HTTPStatusError as e:
        error_message = f"[_perform_sdk_discovery] HTTP Status Error: {e.response.status_code} - {e.response.text}"
        print(error_message)
    except mcp.McpError as e: 
        error_message = f"[_perform_sdk_discovery] MCP Protocol Error: {type(e).__name__} - {e}"
        print(error_message)
    except Exception as e:
        error_message = f"[_perform_sdk_discovery] Error during SDK call: {type(e).__name__} - {e}"
        print(error_message)
        import traceback
        traceback.print_exc()

    if error_message:
        return {"status": "error", "error": error_message}

    # If successful, manifest_data is the CallToolResult from search_servers
    # The actual list of servers is a JSON string within the text content
    if manifest_data and hasattr(manifest_data, 'content') and isinstance(manifest_data.content, list) and len(manifest_data.content) > 0:
        first_content_item = manifest_data.content[0]
        # first_content_item is likely a TextContent model instance
        if hasattr(first_content_item, 'type') and first_content_item.type == "text" and \
           hasattr(first_content_item, 'text') and isinstance(first_content_item.text, str):
            try:
                # The actual list of servers is a JSON string embedded within this text field
                parsed_server_list = json.loads(first_content_item.text)
                return {"status": "success", "manifest": make_json_safe(parsed_server_list)}
            except json.JSONDecodeError as e:
                error_message = f"Failed to parse server list from toolbox response: {e}. Raw text: '{first_content_item.text[:200]}...'"
                print(f"[_perform_sdk_discovery] {error_message}")
                return {"status": "error", "error": error_message}
        else:
            error_message = "Toolbox response content was not in the expected TextContent format."
            print(f"[_perform_sdk_discovery] {error_message} Got item: {first_content_item}")
            return {"status": "error", "error": error_message}
    elif manifest_data and hasattr(manifest_data, 'isError') and manifest_data.isError:
        # Handle cases where the tool call itself returned an error payload
        error_content = "Unknown error from tool."
        if hasattr(manifest_data, 'content') and manifest_data.content and hasattr(manifest_data.content[0], 'text'):
            error_content = manifest_data.content[0].text
        error_message = f"Tool call to Smithery Toolbox reported an error: {error_content}"
        print(f"[_perform_sdk_discovery] {error_message}")
        return {"status": "error", "error": error_message}


    error_message = "Toolbox response was empty or not in the expected format."
    print(f"[_perform_sdk_discovery] {error_message} Manifest data was: {manifest_data}")
    return {"status": "error", "error": error_message}

async def _perform_direct_http_get(base_url: str, path_template: str, query_args: dict, headers: Optional[dict] = None) -> dict:
    # ... (implementation as before)
    query_string = urlencode(query_args)
    full_url = f"{base_url.rstrip('/')}{path_template}?{query_string}" # Ensure path_template starts with /
    print(f"[_perform_direct_http_get] Calling: GET {full_url}")
    # ... (httpx call, return {"status": "success", "result": response.json()} or error dict) ...
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(full_url, headers=headers, timeout=30.0)
            print(f"[_perform_direct_http_get] Response status: {response.status_code}")
            response.raise_for_status()
            return {"status": "success", "result": response.json()} 
    except httpx.HTTPStatusError as e:
        print(f"[_perform_direct_http_get] HTTP error: {e.response.status_code} - {e.response.text}")
        return {"status": "error", "error": f"HTTP error: {e.response.status_code}", "details": e.response.text}
    except Exception as e:
        print(f"[_perform_direct_http_get] Request failed: {str(e)}")
        return {"status": "error", "error": f"Request failed: {str(e)}", "details": traceback.format_exc()}

async def _perform_mcp_tool_execution(protocol: str, endpoint_id: str, tool_name: str, 
                                      tool_args: dict, mcp_url_config: dict, api_key: str) -> dict:
    """
    Internal helper to execute an MCP tool call using the specified protocol.
    mcp_url_config: The dict that will be base64 encoded into the 'config' URL query param (e.g., for Smithery profile).
    """
    print(f'\n-----------\n proto {protocol} endpoint {endpoint_id} args {tool_args} mcp {mcp_url_config} apik {api_key}')
    print(f"[_perform_mcp_tool_execution (Bridge)] Tool: {tool_name}")
    print(f"[_perform_mcp_tool_execution (Bridge)] Tool Args: {json.dumps(tool_args)}")
    print(f"[_perform_mcp_tool_execution (Bridge)] MCP URL Config (from agent): {json.dumps(mcp_url_config)}") # This is what MuseAgent constructed
    
    try:
        result_data = None
        response_dict = None
        if protocol == "direct_http_get":
            base_url = endpoint_id

            path_template = f"/{tool_name.lstrip('/')}" # Ensure leading slash
            custom_headers = mcp_url_config.get("headers", {})
            
            # For direct_http_get, api_key is not used in URL unless explicitly handled by target
            response_dict = await _perform_direct_http_get(base_url, path_template, tool_args, custom_headers)
            return response_dict # This already has status/result or status/error

        if protocol == "websocket":
            # make_ws_url expects the base endpoint_id and mcp_url_config for its 'config_payload'
            result_data = await call_mcp(endpoint_id, mcp_url_config, tool_name, tool_args, api_key)
        elif protocol == "streamablehttp":
            # call_mcp_via_http uses make_mcp_http_service_url, which also takes endpoint_id and mcp_url_config
            result_data = await call_mcp_via_http(endpoint_id, mcp_url_config, tool_name, tool_args, api_key)
        elif protocol == "sse":
            # call_mcp_via_sse uses make_mcp_sse_url
            result_data = await call_mcp_via_sse(endpoint_id, mcp_url_config, tool_name, tool_args, api_key)
        elif protocol == "rest_post":
            # call_tool_via_rest_post takes base_http_url, mcp_service_config (might be mcp_url_config or different), api_key
            # This one might need slight adjustment in how it gets its base URL if endpoint_id is an alias.
            # For now, assuming endpoint_id can be resolved to a base_http_url.
            # The mcp_service_config for call_tool_via_rest_post is not for the URL, but for the MCP session if the tool needs it.
            # Let's assume for now mcp_url_config is not directly used by call_tool_via_rest_post's MCP session itself.
            base_url = endpoint_id # This needs to be the actual HTTP base URL
            if not endpoint_id.startswith("http"): # Basic alias to Smithery URL conversion
                base_url = f"https://server.smithery.ai/{endpoint_id.strip('/')}"

            result_data = await call_tool_via_rest_post(base_url, tool_name, tool_args, {}, api_key)
        else:
            return {"status": "error", "error": f"Unsupported protocol: {protocol}"}

        return {"status": "success", "result": make_json_safe(result_data)}

    except Exception as e:
        error_message = f"Error during MCP tool execution ({protocol}): {type(e).__name__} - {e}"
        print(f"[_perform_mcp_tool_execution] {error_message}")
        # import traceback
        # traceback.print_exc()
        return {"status": "error", "error": error_message, "details": traceback.format_exc()}



async def fetch_github_file_via_bridge(
    github_raw_url: str, 
    fetch_service_config: dict, # Config for @smithery-ai/fetch connection (profile, dynamic, Smithery GW key)
    bridge_smithery_gw_key: str
    ) -> dict:
    """Helper to use the bridge's fetch tool to get a GitHub file."""
    print(f"[GitHubFetch] Attempting to fetch: {github_raw_url}")
    return await _perform_mcp_tool_execution(
        protocol="streamablehttp",
        endpoint_id="@smithery-ai/fetch", # The fetch MCP server
        tool_name="fetch", # Assuming this is its tool name
        tool_args={"url": github_raw_url},
        mcp_url_config=fetch_service_config, # Config for the fetch service itself
        api_key=bridge_smithery_gw_key # Smithery GW key to reach the fetch service
    )

async def parse_github_config_schema(file_content: str, file_type: str = "readme") -> dict | None:
    """
    Attempts to parse service-specific config keys from fetched GitHub file content.
    This will be very heuristic and likely needs refinement.
    file_type can be 'readme', 'json_schema', 'pyproject_toml', 'env_example'
    Returns a dictionary of {config_key_name: "placeholder_for_agent_to_fill"}
    """
    print(f"[GitHubParse] Attempting to parse {file_type} content...")
    required_config_keys = {}
    if not file_content:
        return None

    if file_type == "readme": # Very brittle parsing for README
        # Example: Looking for ```json ... "env": { "API_KEY": ... } ```
        try:
            # This is a placeholder for more robust parsing.
            # For @enescinar/twitter-mcp, the README had a JSON block for Claude Desktop config.
            if '"env": {' in file_content:
                json_block_match = re.search(r'"env":\s*(\{[\s\S]*?\})', file_content)
                if json_block_match:
                    env_config = json.loads(json_block_match.group(1))
                    for key in env_config.keys():
                        # We need to map these ENV_VAR_NAMES to potential camelCase keys
                        # Smithery seems to use for the URL config object. This is a guess.
                        # e.g., TWITTER_API_KEY -> apiKey
                        camel_case_key = key.lower().replace("twitter_", "")
                        parts = camel_case_key.split('_')
                        camel_case_key = parts[0] + "".join(p.capitalize() for p in parts[1:])
                        required_config_keys[camel_case_key] = f"VALUE_FOR_{key}" # Placeholder
                    print(f"[GitHubParse] Extracted from README env block: {required_config_keys}")
                    return required_config_keys
        except Exception as e:
            print(f"[GitHubParse] Could not parse README for env config: {e}")
            
    # Add more parsers for pyproject.toml, .env.example, or a dedicated JSON schema file if found.
    
    if not required_config_keys: # If no specific keys found, return None or an empty dict
        print(f"[GitHubParse] No specific connection config keys extracted from {file_type}.")
        return None # Or {} if we want to indicate an attempt was made but nothing specific found
    return required_config_keys


async def test_dynamic_pipeline(
    global_creds_config: dict, # This will be the loaded test_bridge_config.json
    discovery_query_for_target_server: str, # e.g., "@ThirdGuard/mcp-etherscan-server" or "twitter"
    qname_of_server_to_select: str,         # e.g., "@ThirdGuard/mcp-etherscan-server" (to pick from results)
    tool_name_to_select_and_execute: str,   # e.g., "check-balance"
    tool_execution_args: dict,              # e.g., {"address": "0x..."}
    # How the selected server expects its specific keys in the URL config:
    # Key = name server expects in URL config, Value = key name in global_creds_config
    server_url_config_key_mapping: dict ):

    test_name = f"Test_{qname_of_server_to_select}_{tool_name_to_select_and_execute}"
    print(f"\n--- Running Dynamic Pipeline for: {test_name} ---")

    smithery_gateway_api_key = global_creds_config.get("SMITHERY_GATEWAY_API_KEY")
    smithery_profile_id = global_creds_config.get("SMITHERY_GATEWAY_PROFILE_ID")
    
    if not smithery_gateway_api_key or not smithery_profile_id:
        print("ERROR: Smithery Gateway credentials not in config."); return

    # Config for calling the Smithery Toolbox (discovery step)
    toolbox_url_config = {"dynamic": False, "profile": smithery_profile_id}

    # 1. DISCOVER TARGET SERVER using Smithery Toolbox
    print(f"\nStep 1: Discovering servers matching query: '{discovery_query_for_target_server}'...")
    discovery_result = await _perform_sdk_discovery(
        target_mcp_url_base="server.smithery.ai/@smithery/toolbox", # Hardcoded: Discovery via Smithery Toolbox
        search_tool_name="search_servers",                         # Hardcoded: Tool on Toolbox
        search_args={"query": discovery_query_for_target_server, "n": 5}, # Allow a few results
        config_for_url_param=toolbox_url_config,
        api_key_for_url_param=smithery_gateway_api_key
    )

    print("--- Discovery Result ---"); print(json.dumps(discovery_result, indent=2))
    if discovery_result.get("status") != "success" or not discovery_result.get("manifest"):
        print("Discovery failed. Test cannot proceed."); return
    
    discovered_servers_list = discovery_result["manifest"]
    if not discovered_servers_list:
        print(f"No servers found for query '{discovery_query_for_target_server}'."); return

    # 2. SELECT SERVER AND TOOL from discovery results
    selected_server_manifest = next((s for s in discovered_servers_list if s.get("qualifiedName") == qname_of_server_to_select), None)
    
    if not selected_server_manifest:
        print(f"Target server '{qname_of_server_to_select}' not found in discovery results."); return

    selected_tool_info = next((t for t in selected_server_manifest.get("tools", []) if t.get("name") == tool_name_to_select_and_execute), None)
    
    if not selected_tool_info:
        print(f"Tool '{tool_name_to_select_and_execute}' not found on '{qname_of_server_to_select}'."); return
    print(f"Selected server: {qname_of_server_to_select}, Tool: {selected_tool_info['name']}")

    # 3. PREPARE MCP_URL_CONFIG for the target service execution
    final_mcp_url_config_for_execution = {"dynamic": False, "profile": smithery_profile_id}
    for server_expected_key, key_in_global_creds in server_url_config_key_mapping.items():
        value = global_creds_config.get(key_in_global_creds)
        if value is None:
            print(f"WARNING: Credential '{key_in_global_creds}' for server key '{server_expected_key}' not found in global config.")
        final_mcp_url_config_for_execution[server_expected_key] = value 
    
    print(f"\nFinal mcp_url_config for execution: {final_mcp_url_config_for_execution}")
    print(f"Tool args for execution: {tool_execution_args}")

    # 4. EXECUTE
    print(f"\nExecuting {qname_of_server_to_select}/{tool_name_to_select_and_execute}...")
    execution_result = await _perform_mcp_tool_execution(
        protocol="streamablehttp", # Assuming for Smithery
        endpoint_id=qname_of_server_to_select,
        tool_name=tool_name_to_select_and_execute,
        tool_args=tool_execution_args,
        mcp_url_config=final_mcp_url_config_for_execution,
        api_key=smithery_gateway_api_key
    )
    print(f"--- Execution Result for {test_name} ---"); print(json.dumps(execution_result, indent=2))
    # ... (success/failure print based on execution_result["status"])




async def main():
    redis = await aioredis.from_url(build_redis_url())
    print(f"[bus_to_mcp_bridge] Subscribing to {INBOX_CHANNEL}")
    async def handle_envelope_wrapper(env): # Renamed to avoid conflict if main is in global scope
        # Ensure env is an Envelope object if subscribe doesn't guarantee it
        if not isinstance(env, Envelope):
            try:
                # Assuming env is a dict if not an Envelope object (needs robust parsing)
                env = Envelope.from_dict(env) 
            except Exception as e:
                print(f"[bus_to_mcp_bridge] Error: Received non-Envelope message that couldn't be parsed: {env}, Error: {e}")
                return
        await process_envelope(env, redis)
    
    # Ensure your 'subscribe' function correctly parses the JSON from Redis into an Envelope object
    # before passing it to handle_envelope_wrapper. The log "Received Envelope: Envelope(...)"
    # suggests this is already happening.
    await subscribe(redis, INBOX_CHANNEL, handle_envelope_wrapper, group="mcp_bridge", consumer="mcp_bridge_consumer")
    # subscribe is a blocking call (while True loop), so aclose might not be reached unless subscribe exits
    # Consider try/finally if subscribe can exit.
    # await redis.aclose() # This line might not be reached if subscribe runs forever

# In mcp_bridge_gem.py

BRIDGE_TEST_CONFIG = None #

if __name__ == "__main__":
    
    if os.getenv("RUN_GITHUB_SCHEMA_TEST") == "1true":
        print("--- Testing GitHub Schema Fetching ---")

        test_api_key = SMITHERY_API_KEY 
        test_profile_id = os.getenv("SMITHERY_PROFILE_FOR_TEST", "individual-dragonfly-3acTGr")
        
        if not test_api_key or not test_profile_id:
            print("ERROR: Smithery credentials not set.")
        else:
            # Config for calling @smithery-ai/fetch via the gateway
            fetch_service_mcp_url_config = {
                "dynamic": False,
                "profile": test_profile_id,
                # "smitheryApiKey": test_api_key # Not needed inside b64 config if also a top-level param
            }

            async def run_github_schema_test():
                # --- Part 1: Fetch Smithery Server Page to get GitHub link ---
                # target_smithery_server_page_url = "https://smithery.ai/server/@ThirdGuard/mcp-etherscan-server"
                target_smithery_server_page_url = "https://smithery.ai/server/@rafaljanicki/x-twitter-mcp-server"

                print(f"\nFetching Smithery page: {target_smithery_server_page_url}")
                page_fetch_result = await _perform_mcp_tool_execution(
                    protocol="streamablehttp",
                    endpoint_id="@smithery-ai/fetch",
                    tool_name="fetch", # Assuming this is correct from previous test
                    tool_args={"url": target_smithery_server_page_url, "raw": True},
                    mcp_url_config=fetch_service_mcp_url_config,
                    api_key=test_api_key
                )

                if page_fetch_result.get("status") != "success":
                    print("Failed to fetch Smithery server page.")
                    print(json.dumps(page_fetch_result, indent=2))
                    return

                html_content = ""
                page_content_list = page_fetch_result.get("result", {}).get("content", [])
                if page_content_list and page_content_list[0].get("type") == "text":
                    html_content = page_content_list[0]["text"]
                
                if not html_content:
                    print("No HTML content from Smithery page fetch."); return

                # --- Part 2: Parse HTML for GitHub URL ---
                qname_for_heuristic = None
                if "smithery.ai/server/" in target_smithery_server_page_url:
                    qname_for_heuristic = target_smithery_server_page_url.split("smithery.ai/server/")[-1]
                print(f'[qname] {qname_for_heuristic}')
                github_repo_url = extract_github_url_from_html(html_content, qname_for_heuristic)
                #github_repo_url = extract_github_url_from_html(html_content)
                if not github_repo_url:
                    print(f"Could not extract GitHub repo URL from {target_smithery_server_page_url}"); return
                
                print(f"Extracted GitHub Repo URL: {github_repo_url}")

                # --- Part 3: Fetch smithery.yaml from GitHub ---
                # This uses the _fetch_raw_github_file helper which we'll define/refine
                # For direct testing here, _fetch_raw_github_file can use httpx directly.
                
                # For the purpose of this direct test, let's make _fetch_raw_github_file use httpx
                # if it's called from here, bypassing the need for _perform_mcp_tool_execution for this sub-step.
                
                async def _direct_fetch_github_file(raw_url: str) -> str | None:
                    try:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(raw_url, follow_redirects=True, timeout=10.0)
                            if response.status_code == 200:
                                print(f"Successfully fetched {raw_url}")
                                return response.text
                            elif response.status_code == 404: # Try 'master' if 'main' fails
                                if "/main/" in raw_url:
                                    print(f"File not found with /main/, trying /master/ for {raw_url}")
                                    master_url = raw_url.replace("/main/", "/master/")
                                    response = await client.get(master_url, follow_redirects=True, timeout=10.0)
                                    if response.status_code == 200:
                                        print(f"Successfully fetched {master_url}")
                                        return response.text
                                    else:
                                        print(f"Failed to fetch {master_url}: {response.status_code}")
                                        return None
                                else:
                                    print(f"Failed to fetch {raw_url}: {response.status_code}")
                                    return None
                            else:
                                print(f"Failed to fetch {raw_url}: {response.status_code}")
                                return None
                    except Exception as e:
                        print(f"Error fetching {raw_url}: {e}")
                        return None

                parsed_gh_url = urlparse(github_repo_url)
                gh_path_parts = [part for part in parsed_gh_url.path.split('/') if part]
                if len(gh_path_parts) < 2:
                    print("Could not parse owner/repo from GitHub URL."); return
                
                owner, repo = gh_path_parts[0], gh_path_parts[1]
                raw_yaml_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/smithery.yaml" # Try main first
                
                print(f"Attempting to fetch: {raw_yaml_url}")
                yaml_content = await _direct_fetch_github_file(raw_yaml_url)
                
                if not yaml_content:
                    print(f"Failed to fetch smithery.yaml from {github_repo_url}"); return

                print("--- Fetched smithery.yaml Content (first 500 chars) ---")
                print(yaml_content[:500])

                # --- Part 4: Parse YAML for configSchema ---
                connection_config_schema = _parse_smithery_yaml_for_config_schema(yaml_content)
                if connection_config_schema:
                    print("\n--- Extracted Connection Config Schema ---")
                    print(json.dumps(connection_config_schema, indent=2))
                    required_keys = connection_config_schema.get("required", [])
                    print(f"Required keys for URL config: {required_keys}")
                else:
                    print("Could not parse configSchema from smithery.yaml.")
            
            asyncio.run(run_github_schema_test())

    else:
        print("Running bridge main (AetherBus listener). To run tests, set an appropriate RUN_..._TEST environment variable.")
    
    #sys.exit()
    # REMOVE ALL RUN_..._TEST blocks or ensure only one is active for focused testing.
    # For deploying the bridge, this __main__ should ONLY call asyncio.run(main())

    # Example: To run the bridge as an AetherBus service:
    print("Starting MCP Bridge AetherBus service...")
    asyncio.run(main())

    # To run a specific test (ensure only one of these test env vars is true at a time):
    # if os.getenv("RUN_GITHUB_FETCH_TEST") == "true":
    #     if not BRIDGE_TEST_CONFIG: print("test_bridge_config.json not loaded.")
    #     else: asyncio.run(test_github_fetch_logic_here(BRIDGE_TEST_CONFIG)) # You'd define this
    # elif ... other specific test modes ...