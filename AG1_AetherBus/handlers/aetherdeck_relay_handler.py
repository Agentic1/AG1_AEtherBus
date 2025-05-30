# relay_server.py (Now the AetherDeck Edge Handler)
import aiohttp_cors
from aiohttp import web, WSMsgType, WSCloseCode
import uuid
import asyncio
import json
from dotenv import load_dotenv
import redis.asyncio as aioredis # Added Redis import here
import os
import functools
import argparse # Added for main execution block
import datetime # For registration timestamp

# Assuming your AG1_AetherBus is in a discoverable path
from AG1_AetherBus.bus import publish_envelope, build_redis_url, subscribe
from AG1_AetherBus.keys import StreamKeyBuilder # Make sure this is correctly importable
from AG1_AetherBus.envelope import Envelope

# Ensure Redis is imported for type hinting and use
from redis.asyncio import Redis # Explicit import

load_dotenv()

API_KEY = os.getenv("RELAY_API_KEY", "supersecret")
REDIS_URL = build_redis_url()
REQUIRE_WS_API_KEY = os.getenv("REQUIRE_WS_API_KEY", "false").lower() == "true"

# --- Global state for WebSocket connections ---
active_websockets = {}  # user_id -> WebSocketResponse object
kb = StreamKeyBuilder() # Initialize once

ALLOWED_ORIGINS = ["*"] # Keep this for now for flexibility during testing

# --- Agent Registry for AetherDeck traffic ---
# { "aetherdeck_user_id_pattern" (e.g., "all" or specific user_id) :
#   { "agent_name": str, "agent_inbox_stream": str, "timestamp": str }
# }
aetherdeck_registered_agents = {} 

# --- StreamKeyBuilder Extension (Conceptual) ---
# Removed placeholder for aetherdeck_register as we'll use kb.edge_register("aetherdeck")
# Removed placeholder for aetherdeck_directives_user_inbox as we'll use kb.edge_response("aetherdeck", user_id)


# --- Handle Incoming AetherDeck Registrations (from agents like MuseAgent) ---
async def handle_aetherdeck_registration_envelope(env: Envelope, redis_client: Redis):
    """
    Processes 'register' envelopes specific to AetherDeck,
    updating the internal registry of agents that handle AetherDeck traffic.
    """
    if env.envelope_type == "register" and env.content.get("channel_type") == "aetherdeck":
        agent_name = env.agent_name
        aetherdeck_user_id_pattern = env.content.get("aetherdeck_user_id_pattern") # This allows registering for "all" users or specific ones
        agent_inbox_stream = env.content.get("agent_inbox_stream")
        timestamp = env.timestamp

        if agent_name and aetherdeck_user_id_pattern and agent_inbox_stream:
            aetherdeck_registered_agents[aetherdeck_user_id_pattern] = {
                "agent_name": agent_name,
                "agent_inbox_stream": agent_inbox_stream,
                "timestamp": timestamp
            }
            print(f"[AETHERDECK_HANDLER][REGISTER] Registered agent '{agent_name}' for AetherDeck user pattern '{aetherdeck_user_id_pattern}'. Will forward events to: {agent_inbox_stream}")
        else:
            print(f"[AETHERDECK_HANDLER][REGISTER][WARN] Invalid AetherDeck registration envelope: {env}")
    else:
        print(f"[AETHERDECK_HANDLER][REGISTER][INFO] Ignoring non-AetherDeck registration or non-register envelope: {env.envelope_type}, channel_type: {env.content.get('channel_type')}")


# --- WebSocket Handling Logic ---

async def handle_aetherdeck_event(user_id, session_code, raw_message_data, redis_client, ws_connection):
    """
    Handles an incoming event from an AetherDeck client (via WebSocket),
    wraps it in an Envelope, and publishes it to the registered agent's inbox.
    """
    try:
        event_data = json.loads(raw_message_data)
        print(f"[RELAY-WS][EVENT] User {user_id} Session {session_code} Event: {event_data}")

        # 1. Find the target agent's inbox based on registration
        target_agent_info = aetherdeck_registered_agents.get(user_id) # Try exact user_id match first
        if not target_agent_info and "all" in aetherdeck_registered_agents: # Fallback to "all" if available
            target_agent_info = aetherdeck_registered_agents.get("all")

        if not target_agent_info:
            error_msg = f"No agent registered to handle AetherDeck traffic for user '{user_id}' (or 'all' users)."
            print(f"[RELAY-WS][ERROR] {error_msg}")
            # Send a UI directive to inform the AetherDeck user
            await ws_connection.send_json({
                "directive_type": "SHOW_NOTIFICATION",
                "message": f"No agent currently available to handle your AetherDeck request for user ID '{user_id}'. Please try again later or contact support.",
                "notification_type": "error"
            })
            return

        target_agent_inbox_stream = target_agent_info["agent_inbox_stream"]
        print(f"[RELAY-WS][TARGET] Event for User {user_id} routed to registered agent's inbox: {target_agent_inbox_stream}")

        # This is the stream where agents on the bus should send UI Directives for *this specific AetherDeck client*.
        # The relay's `listen_for_user_directives` will be subscribed to this stream.
        reply_to_stream = kb.edge_response("aetherdeck", user_id) # USE edge_response pattern
        print(f"[RELAY-WS][REPLY_TO] UI Directives for {user_id} expected on: {reply_to_stream}")

        # --- IMPORTANT: Normalize AetherDeck event payload into a single 'text' for the agent ---
        text_for_agent_input = ""
        source_event_type = event_data.get("event_type")
        payload = event_data.get("payload", {})

        if source_event_type == "user_chat_input":
            #text_for_agent_input = payload.get("text", "").strip()
            text_for_agent_input = event_data.get("text", "").strip() # Access 'text' directly from event_data
            print(f"[RELAY-WS][NORMALIZED] User chat input: '{text_for_agent_input[:100]}...'")
        elif source_event_type == "component_interaction":
            component_id = event_data.get("component_id", "N/A")
            event_name = event_data.get("event_name", "N/A")
            # Format this for the LLM to understand as an interaction
            text_for_agent_input = (
                f"User interacted with AetherDeck UI: clicked '{event_name}' on component '{component_id}'. "
                f"Payload: {json.dumps(event_data.get('payload', {}))}"
            )
            print(f"[RELAY-WS][NORMALIZED] Component interaction: '{text_for_agent_input[:100]}...'")
        elif source_event_type == "ui_event":
            action = event_data.get("action", "N/A")
            window_id = event_data.get("window_id", "N/A")
            text_for_agent_input = f"AetherDeck UI event: '{action}' for Window ID: '{window_id}'."
            print(f"[RELAY-WS][NORMALIZED] UI event: '{text_for_agent_input[:100]}...'")
        else:
            text_for_agent_input = f"[AetherDeck Event: {source_event_type}] Raw payload: {json.dumps(event_data)}"
            print(f"[RELAY-WS][NORMALIZED] Unknown AetherDeck event: '{text_for_agent_input[:100]}...'")

        """envelope_content = {
            "source": "aetherdeck", # Indicates to consuming agents that this is an AetherDeck event
            "event_type": event_data.get("event_type"), # e.g., "user_chat_input", "component_interaction"
            "payload": event_data # The raw AetherDeck event payload
        }"""

        envelope = Envelope(
            role="user_interface_event", # General role for UI events
            user_id=user_id,
            session_code=session_code,
            reply_to=reply_to_stream, # Crucial: this is the reply stream for UI Directives to this specific WS client
            #content=envelope_content, # Old way
            content={"text": text_for_agent_input, "source_channel": "aetherdeck"},
            
            agent_name="aetherdeck_relay_ws", # Name of this relay component
            envelope_type="event", # General envelope type for these UI interactions
            correlation_id=str(uuid.uuid4()) # New correlation ID for this bus transaction
        )
        
        await publish_envelope(redis_client, target_agent_inbox_stream, envelope) # Publish to the agent's specific inbox
        print(f"[RELAY-WS][PUBLISHED] Event from User {user_id} (Session: {session_code}) to Agent Inbox: {target_agent_inbox_stream}, Reply-To for UI Directives: {reply_to_stream}")

    except json.JSONDecodeError:
        error_msg = f"Invalid JSON received from AetherDeck client {user_id}"
        print(f"[RELAY-WS][ERROR] {error_msg}: {raw_message_data}")
        try:
            await ws_connection.send_json({"error": "Invalid JSON", "type": "protocol_error"})
        except Exception as e_send:
            print(f"[RELAY-WS][ERROR] Failed to send JSON error to client {user_id}: {e_send}")
    except Exception as e:
        error_msg = f"Error handling AetherDeck event from {user_id}"
        print(f"[RELAY-WS][ERROR] {error_msg}: {e}")
        try:
            await ws_connection.send_json({"error": str(e), "type": "server_error"})
        except Exception as e_send:
            print(f"[RELAY-WS][ERROR] Failed to send server error to client {user_id}: {e_send}")


async def listen_for_user_directives(user_id, ws_connection, redis_client):
    """
    Listens on the AetherBus for UI Directives targeted at a specific AetherDeck user
    and sends them over the WebSocket connection.
    """
    directive_stream = kb.edge_response("aetherdeck", user_id) # USE edge_response pattern
    print(f"[RELAY-WS][SUBSCRIBE] Listening on Bus Stream {directive_stream} for AetherDeck directives for User {user_id}")

    # Use a unique group name for each user's listener to prevent message duplication
    group_name = f"aetherdeck_ws_listener_{user_id}"
    try:
        await redis_client.xgroup_create(directive_stream, group_name, mkstream=True, id='$') # Ensure group exists
    except Exception as e:
        # This can fail if group already exists, which is fine. Log other errors.
        if "BUSYGROUP" not in str(e):
            print(f"[RELAY-WS][XGROUP_CREATE][WARN] Failed to create XGroup for {directive_stream} with group {group_name}: {e}")


    # ---> Content handler Start !!!
    async def directive_envelope_handler(envelope: Envelope):
        print(f"[RELAY-WS][DIRECTIVE] Received Envelope on {directive_stream} for User {user_id}. Content: {envelope.content}")
        if not ws_connection.closed:
            try:
                # Assuming envelope.content is the AetherDeck UI Directive dictionary
                # as defined in AetherDeck's README.md
                if isinstance(envelope.content, dict) and "directive_type" in envelope.content:
                    await ws_connection.send_json(envelope.content)
                    print(f"[RELAY-WS][SENT-DIRECTIVE] Sent UI Directive to AetherDeck User {user_id}: {envelope.content.get('directive_type')}")
                
                elif isinstance(envelope.content, dict) and "text" in envelope.content:
                        # It's a plain text message from the agent, wrap it in an APPEND_TO_TEXT_DISPLAY directive
                        text_to_display = envelope.content["text"]
                        # Assume a main chat window exists and append to its chat_log
                        await ws_connection.send_json({
                            "directive_type": "APPEND_TO_TEXT_DISPLAY",
                            "window_id": "main_chat_window", # Assuming a main chat window with this ID
                            "component_id": "chat_log",      # Assuming a chat log component within it
                            "content_to_append": f"Agent: {text_to_display}\n" # Prefix agent message
                        })
                        print(f"[RELAY-WS][SENT-TEXT-WRAP] Sent text as APPEND_TO_TEXT_DISPLAY to {user_id}: {text_to_display[:50]}...")
                else:
                    print(f"[RELAY-WS][WARN] Received unhandled content format for User {user_id} on {directive_stream}: {envelope.content}. Expected a UI Directive or text content.")
                    await ws_connection.send_json({
                        "directive_type": "SHOW_NOTIFICATION",
                        "message": f"Agent sent unhandled response format: {str(envelope.content)[:100]}",
                        "notification_type": "error"
                    })
            except Exception as e:
                print(f"[RELAY-WS][ERROR] Failed to send UI Directive to AetherDeck User {user_id} via WebSocket: {e}")



             ##------>>>>---->>>> Content Handling end!!!   
        else:
            print(f"[RELAY-WS][WARN] WebSocket for User {user_id} is closed. Cannot send UI Directive. Directive Stream: {directive_stream}")
            # This listener will automatically terminate when the main websocket_handler task ends.

    try:
        # CORRECTED: Pass redis_client, stream_name, and handler positionally
        await subscribe(redis_client, directive_stream, directive_envelope_handler, group=group_name)
    except asyncio.CancelledError:
        print(f"[RELAY-WS][UNSUBSCRIBE] Listener for User {user_id} on {directive_stream} cancelled.")
    except Exception as e:
        print(f"[RELAY-WS][ERROR] Subscriber for User {user_id} on {directive_stream} crashed: {e}")
    finally:
        print(f"[RELAY-WS][STOPPED] Stopped listening on Bus Stream {directive_stream} for User {user_id}")


async def websocket_handler(request):
    """
    Handles incoming WebSocket connections from AetherDeck clients.
    """
    # FIX: Specify supported protocols for proper negotiation
    ws = web.WebSocketResponse(protocols=('json',)) 
    try:
        await ws.prepare(request)
    except Exception as e:
        print(f"[WS-PREPARE][ERROR] WebSocket preparation failed for {request.remote}: {e}")
        return web.Response(status=400, text=f"WebSocket handshake failed: {e}")


    # --- User/Session Identification & Authorization ---
    user_id = request.query.get("user_id")
    session_code = request.query.get("session_code") # Optional
    client_api_key = request.query.get("api_key")

    if not user_id:
        # Generate a default user_id if not provided by the client, for unauthenticated use
        user_id = f"aetherdeck_guest_{str(uuid.uuid4())[:8]}" 
        print(f"[WS][INFO] No user_id provided by {request.remote}. Assigned default: {user_id}")

    if REQUIRE_WS_API_KEY:
        if not client_api_key or client_api_key != API_KEY:
            print(f"[WS][AUTH-FAIL] Unauthorized WS connection attempt from {request.remote}. API Key: '{client_api_key}'")
            await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b'Unauthorized: Invalid or missing API key.')
            return ws

    # Check for existing connection for this user_id
    # Policy: Close existing connection if a new one for the same user_id arrives.
    if user_id in active_websockets:
        print(f"[WS][WARN] User {user_id} already connected. Closing previous connection for this user_id.")
        try:
            await active_websockets[user_id].close(code=WSCloseCode.GOING_AWAY, message=b'New connection for this user_id.')
        except Exception as e_close_old:
            print(f"[WS][WARN] Error closing old WebSocket for {user_id}: {e_close_old}")
        if user_id in active_websockets: # Check again in case close was problematic
             del active_websockets[user_id]


    print(f"[WS][CONNECT] AetherDeck client connected: UserID='{user_id}', SessionCode='{session_code}', Remote='{request.remote}'")
    active_websockets[user_id] = ws
    
    redis_client = request.app['redis_pool'] # Get redis from app context
    bus_listener_task = None # Initialize

    try:
        #---->>>>> ADDD WINDOW LOGIC !!!
        # --- NEW: Send initial CREATE_WINDOW directive on new connection ---
        initial_chat_window_directive = {
            "directive_type": "CREATE_WINDOW",
            "window_id": "main_chat_window",
            "title": f"AetherDeck Chat ({user_id})", # Dynamic title
            "initial_components": [
                {
                    "component_id": "chat_log",
                    "component_type": "text_display",
                    "content": "Chat session started. Type your message below.\n"
                }
            ],
            "position": {"x": 100, "y": 100},
            "size": {"width": 450, "height": 350}
        }
        try:
            await ws.send_json(initial_chat_window_directive)
            print(f"[WS][UI] Sent CREATE_WINDOW for user {user_id}: main_chat_window")
        except Exception as e:
            print(f"[WS][UI][ERROR] Failed to send CREATE_WINDOW to user {user_id}: {e}")
            # Consider closing the WS if this fails, as UI won't be functional.

        bus_listener_task = asyncio.create_task(listen_for_user_directives(user_id, ws, redis_client))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Example: allow client to send 'close' to cleanly close WS
                if msg.data == 'close': 
                    await ws.close()
                else:
                    # Forward AetherDeck client message to the bus
                    await handle_aetherdeck_event(user_id, session_code, msg.data, redis_client, ws)
            elif msg.type == WSMsgType.ERROR:
                print(f'[WS][ERROR] WebSocket connection for User {user_id} closed with exception {ws.exception()}')
            elif msg.type == WSMsgType.CLOSED: 
                print(f'[WS][CLOSED] Closed message received for User {user_id}') 
                break # Exit the loop as the socket is closed.

    except asyncio.CancelledError:
        print(f"[WS][CANCELLED] WebSocket handler task for User {user_id} was cancelled.")
        raise # Re-raise to ensure cleanup
    except Exception as e:
        print(f"[WS][UNHANDLED-EXC] Unhandled exception in WebSocket handler for User {user_id}: {e}")
    finally:
        print(f"[WS][DISCONNECT] AetherDeck client disconnecting procedures for UserID='{user_id}'.")
        if user_id in active_websockets and active_websockets[user_id] is ws:
            del active_websockets[user_id]
        
        if bus_listener_task and not bus_listener_task.done():
            print(f"[WS][CLEANUP] Cancelling bus listener task for User {user_id}.")
            bus_listener_task.cancel()
            try:
                await bus_listener_task
            except asyncio.CancelledError:
                print(f"[WS][CLEANUP] Bus listener task for User {user_id} cancelled cleanly.")
            except Exception as e_task_cleanup:
                print(f"[WS][CLEANUP-ERROR] Exception during bus_listener_task cleanup for {user_id}: {e_task_cleanup}")
        
        # Ensure WebSocket is closed if not already
        if not ws.closed:
            await ws.close()
        print(f"[WS][DISCONNECTED] AetherDeck client UserID='{user_id}' fully disconnected.")
    
    return ws


# --- Existing HTTP Endpoints (from original relay_server.py) ---
# Modified to use request.app['redis_pool']

async def send_message_oneway(request):
    print(f'[RELAY][HTTP-SendOneWay] {request}')
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")
    try:
        data = await request.json()
        user_id = data.get("user_id")
        text = data.get("text")
        target_stream = data.get("target") or kb.user_inbox(user_id) # Example target

        if not user_id or not text:
            return web.Response(status=400, text="Missing user_id or text")

        envelope = Envelope(
            role="user", user_id=user_id,
            reply_to=kb.user_inbox_response(user_id), # Example reply_to
            content={"text": text},
            agent_name="http_relay_oneway", envelope_type="message"
        )
        await publish_envelope(request.app['redis_pool'], target_stream, envelope)
        return web.json_response({"status": "sent", "target_stream": target_stream})
    except Exception as e:
        return web.Response(status=500, text=f"Error: {str(e)}")

async def wait_for_reply(redis_client, correlation_id, reply_to_stream, timeout=3):
    print(f"[RelayWaiter] Waiting for reply: correlation_id={correlation_id}, reply_to={reply_to_stream}")
    q = asyncio.Queue()
    async def handler(envelope):
        if hasattr(envelope, "correlation_id") and envelope.correlation_id == correlation_id:
            await q.put(envelope.to_dict())
    # CORRECTED: Pass handler positionally
    listener_task = asyncio.create_task(subscribe(redis_client, reply_to_stream, handler))
    try:
        reply = await asyncio.wait_for(q.get(), timeout=timeout)
        return reply
    except asyncio.TimeoutError:
        return None
    finally:
        listener_task.cancel()
        try: await listener_task
        except asyncio.CancelledError: pass

async def send_message(request): # HTTP endpoint for request-response
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")
    try:
        data = await request.json()
    except Exception: return web.Response(status=400, text="Invalid JSON body")

    user_id = data.get("user_id", "unknown_user")
    text = data.get("text", "")
    session_code = data.get("session_code")
    agent_name = data.get("agent_name", "pa0") # Default agent
    correlation_id = str(uuid.uuid4())

    if session_code:
        target_stream = kb.flow_input(session_code)
        reply_to = kb.flow_output(session_code) # Replies from flow come here
    else:
        target_stream = kb.agent_inbox(agent_name)
        reply_to = kb.user_inbox_response(user_id) # Direct agent replies come here

    envelope = Envelope(
        role="user", user_id=user_id, agent_name=agent_name,
        reply_to=reply_to, correlation_id=correlation_id, session_code=session_code,
        content={"text": text}, envelope_type="message"
    )
    
    await publish_envelope(request.app['redis_pool'], target_stream, envelope)
    
    print(f"[RELAY][HTTP-Send] Sent to {target_stream}, waiting on {reply_to} (CID: {correlation_id})")
    # CORRECTED: Pass handler positionally
    result = await wait_for_reply(request.app['redis_pool'], correlation_id, reply_to, timeout=10)

    if result:
        return web.json_response(result)
    else: # Timeout or no matching reply
        return web.json_response({
            "status": "sent_no_reply_within_timeout", "correlation_id": correlation_id,
            "text": text, "user_id": user_id, "reply_to_waited_on": reply_to,
            "target_stream": target_stream
        }, status=202) # Accepted, but processing might be ongoing

async def poll_messages(request): # HTTP polling endpoint
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")
    user_id = request.query.get("user_id")
    since = request.query.get("since", "-") # Redis stream ID or '-' for earliest
    flow = request.query.get("flow") or request.query.get("session_code")

    if flow: stream_to_poll = kb.flow_output(flow)
    elif user_id: stream_to_poll = kb.user_inbox(user_id) # Or user_inbox_response
    else: return web.Response(status=400, text="Missing flow or user_id for polling")

    try:
        messages = await request.app['redis_pool'].xrange(stream_to_poll, since, "+", count=100)
        parsed = []
        for msg_id, entry in messages:
            try:
                data = json.loads(entry.get(b"data", b"{}").decode())
                data["_stream_message_id"] = msg_id.decode() # Add stream ID for client tracking
                parsed.append(data)
            except Exception: continue # Skip malformed messages
        return web.json_response(parsed)
    except Exception as e:
        return web.Response(status=500, text=f"Error reading stream {stream_to_poll}: {e}")

# --- App Setup ---
async def on_startup_redis(app):
    """Initialize Redis connection pool on app startup."""
    print(f"[APP-LIFECYCLE] Connecting to Redis at {REDIS_URL}")
    app['redis_pool'] = await aioredis.from_url(REDIS_URL, decode_responses=True)
    print("[APP-LIFECYCLE] Redis connection pool established.")

async def on_cleanup_redis(app):
    """Close Redis connection pool on app cleanup."""
    if 'redis_pool' in app and app['redis_pool']:
        print("[APP-LIFECYCLE] Closing Redis connection pool...")
        await app['redis_pool'].close()
        print("[APP-LIFECYCLE] Redis connection pool closed.")

@web.middleware
async def cors_middleware(request, handler):
    request_origin = request.headers.get("Origin")
    
    # Determine the Access-Control-Allow-Origin header value
    # If the request_origin is in ALLOWED_ORIGINS, use it. Otherwise, fall back to "*".
    # This logic allows specific origins while still being permissive if "*" is in ALLOWED_ORIGINS.
    # For strict production, ALLOWED_ORIGINS should NOT contain "*" and this logic would be simpler.
    allow_origin_header = "*"
    if request_origin and request_origin in ALLOWED_ORIGINS:
        allow_origin_header = request_origin
    elif "*" in ALLOWED_ORIGINS: # If "*" is explicitly allowed, then allow any origin.
        allow_origin_header = "*"
    else: # If "*" is not allowed, and the specific origin isn't, then don't set the header (or set to a default allowed one)
        # For stricter security, you might not set the header at all, or set it to the first allowed origin.
        # For now, we'll stick to the previous behavior of allowing all if "*" is in ALLOWED_ORIGINS.
        pass # No change to allow_origin_header, it remains "*" if not explicitly matched and "*" is in ALLOWED_ORIGINS.


    # For preflight requests (OPTIONS method)
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": allow_origin_header, # Use the determined header value
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS, PUT, DELETE, PATCH",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Max-Age": "86400" # Cache preflight for 24 hours
        }
        return web.Response(status=200, headers=headers)

    response = await handler(request)
    
    # For actual requests
    response.headers["Access-Control-Allow-Origin"] = allow_origin_header # Use the determined header value
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE, PATCH"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response

app = web.Application(middlewares=[cors_middleware])
# These app lifecycle hooks will be appended by the main execution block below

# CORS setup for specific routes (more granular if needed, or defaults)
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
        allow_methods="*" # Allow all methods for simplicity here
    )
})

# Add WebSocket Route (now on root path "/")
ws_route = app.router.add_get("/", websocket_handler) # Now listening on root path
cors.add(ws_route) # Ensure CORS applies to the WebSocket route for handshake

# Add existing HTTP Routes
http_send_oneway_route = app.router.add_post("/send_oneway", send_message_oneway) # Renamed for clarity
http_send_route = app.router.add_post("/send", send_message)
http_poll_route = app.router.add_get("/poll", poll_messages)

cors.add(http_send_oneway_route)
cors.add(http_send_route)
cors.add(http_poll_route)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start AetherDeck Edge Handler (Relay).")
    args, unknown = parser.parse_known_args() 

    async def start_all_services():
        # Ensure redis_pool is available for aiohttp handlers via on_startup
        app.on_startup.append(on_startup_redis)
        app.on_cleanup.append(on_cleanup_redis)
        
        # Setup Aiohttp App Runner and Site first, but don't await site.start() yet
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Now that app['redis_pool'] is initialized via on_startup_redis (triggered by runner.setup())
        redis_for_subscriptions = app['redis_pool'] 

        # Start the bus subscription for AetherDeck agent registrations as a background task
        # This task will continuously listen for new agent registrations
        registration_patterns = [kb.edge_register("aetherdeck")] # Using existing pattern
        print(f"[AETHERDECK_HANDLER][INIT] Subscribing to agent registration stream: {registration_patterns}")
        
        # CORRECTED: Pass redis_client, stream_name, and handler positionally
        for stream_pattern in registration_patterns:
            asyncio.create_task(
                subscribe( 
                    redis_for_subscriptions, # Positional arg 1
                    stream_pattern,          # Positional arg 2
                    functools.partial(handle_aetherdeck_registration_envelope, redis_client=redis_for_subscriptions), # Positional arg 3
                    group="aetherdeck_registration_listener" # Keyword arg
                )
            )
        
        # Give a moment for registration listener to set up (optional, just for cleaner logs)
        await asyncio.sleep(0.5) 
        print("[AETHERDECK_HANDLER][INIT] Agent registration listener started.")

        # Start the aiohttp web application (this will now be the main blocking call)
        listen_port = int(os.getenv("RELAY_PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', listen_port)
        await site.start()

        print(f"[INIT] Starting AetherRelay Server...")
        print(f"[INIT] API_KEY loaded (first 5 chars): {API_KEY[:5]}...")
        print(f"[INIT] REQUIRE_WS_API_KEY: {REQUIRE_WS_API_KEY}")
        print(f"[INIT] Redis URL: {REDIS_URL}")
        print(f"[INIT] HTTP endpoints available at /send, /send_oneway, /poll")
        print(f"[INIT] WebSocket endpoint available at /") # Corrected path
        print(f"======== Running on http://0.0.0.0:{listen_port} ========")

        # Keep the main task alive indefinitely. This is where the event loop effectively runs.
        await asyncio.Future()

    try:
        asyncio.run(start_all_services())
    except KeyboardInterrupt:
        print("\n[AETHERDECK_HANDLER] Server shutting down (KeyboardInterrupt received).")
        # aiohttp's runner.cleanup() handles closing connections etc. implicitly on shutdown.
    except Exception as e:
        print(f"[AETHERDECK_HANDLER] Server failed to start or run: {e}")
        import traceback
        traceback.print_exc() # Print full traceback for unhandled exceptions
    sys.exit(0)