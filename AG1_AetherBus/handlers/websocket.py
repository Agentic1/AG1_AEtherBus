import asyncio
import json
import uuid
from aiohttp import web, WSMsgType, WSCloseCode
from functools import partial

from AG1_AetherBus.bus import publish_envelope, subscribe
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

from .registrations import aetherdeck_registered_agents
from .server import kb, REQUIRE_WS_API_KEY, API_KEY

# Active WebSocket connections indexed by user_id
active_websockets = {}

async def handle_aetherdeck_event(user_id: str, session_code: str, raw_message_data: str, redis_client, ws_connection):
    """Forward events from the AetherDeck client to the registered agent."""
    try:
        event_data = json.loads(raw_message_data)
        print(f"[RELAY-WS][EVENT] User {user_id} Session {session_code} Event: {event_data}")

        target_agent_info = aetherdeck_registered_agents.get(user_id)
        if not target_agent_info and "all" in aetherdeck_registered_agents:
            target_agent_info = aetherdeck_registered_agents.get("all")

        if not target_agent_info:
            await ws_connection.send_json({
                "directive_type": "SHOW_NOTIFICATION",
                "message": f"No agent currently available to handle your AetherDeck request for user ID '{user_id}'. Please try again later or contact support.",
                "notification_type": "error",
            })
            print(f"[RELAY-WS][ERROR] No agent registered for user {user_id}")
            return

        target_stream = target_agent_info["agent_inbox_stream"]
        reply_to_stream = kb.edge_response("aetherdeck", user_id)

        text_for_agent = ""
        source_event_type = event_data.get("event_type")

        if source_event_type == "user_chat_input":
            text_for_agent = event_data.get("text", "").strip()
        elif source_event_type == "component_interaction":
            component_id = event_data.get("component_id", "N/A")
            event_name = event_data.get("event_name", "N/A")
            text_for_agent = (
                f"User interacted with AetherDeck UI: clicked '{event_name}' on component '{component_id}'. "
                f"Payload: {json.dumps(event_data.get('payload', {}))}"
            )
        elif source_event_type == "ui_event":
            action = event_data.get("action", "N/A")
            window_id = event_data.get("window_id", "N/A")
            text_for_agent = f"AetherDeck UI event: '{action}' for WindowID: '{window_id}'."
        else:
            text_for_agent = f"[AetherDeck Event: {source_event_type}] Raw payload: {json.dumps(event_data)}"

        envelope = Envelope(
            role="user_interface_event",
            user_id=user_id,
            session_code=session_code,
            reply_to=reply_to_stream,
            content={"text": text_for_agent, "source_channel": "aetherdeck"},
            agent_name="aetherdeck_relay_ws",
            envelope_type="event",
            correlation_id=str(uuid.uuid4()),
        )
        await publish_envelope(redis_client, target_stream, envelope)
        print(
            f"[RELAY-WS][PUBLISHED] Event from User {user_id} (Session: {session_code}) to Agent Inbox: {target_stream}, Reply-To for UI Directives: {reply_to_stream}"
        )
    except json.JSONDecodeError:
        await ws_connection.send_json({"error": "Invalid JSON", "type": "protocol_error"})
    except Exception as e:
        await ws_connection.send_json({"error": str(e), "type": "server_error"})


async def listen_for_user_directives(user_id: str, ws_connection, redis_client):
    """Listen for UI directives targeted at the specific WebSocket user."""
    directive_stream = kb.edge_response("aetherdeck", user_id)
    group_name = f"aetherdeck_ws_listener_{user_id}"
    try:
        await redis_client.xgroup_create(directive_stream, group_name, mkstream=True, id="$")
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            print(f"[RELAY-WS][XGROUP_CREATE][WARN] Failed to create XGroup for {directive_stream} with group {group_name}: {e}")

    async def handler(envelope: Envelope):
        print(f"[RELAY-WS][DIRECTIVE] Received Envelope on {directive_stream} for User {user_id}. Content: {envelope.content}")
        if ws_connection.closed:
            print(f"[RELAY-WS][WARN] WebSocket for User {user_id} is closed. Cannot send UI Directive.")
            return
        try:
            if isinstance(envelope.content, dict) and "directive_type" in envelope.content:
                await ws_connection.send_json(envelope.content)
                print(f"[RELAY-WS][SENT-DIRECTIVE] Sent UI Directive to AetherDeck User {user_id}: {envelope.content.get('directive_type')}")
            elif isinstance(envelope.content, dict) and "text" in envelope.content:
                text_to_display = envelope.content["text"]
                await ws_connection.send_json({
                    "directive_type": "APPEND_TO_TEXT_DISPLAY",
                    "window_id": "main_chat_window",
                    "component_id": "chat_log",
                    "content_to_append": f"Agent: {text_to_display}\n",
                })
                print(f"[RELAY-WS][SENT-TEXT-WRAP] Sent text as APPEND_TO_TEXT_DISPLAY to {user_id}: {text_to_display[:50]}...")
            else:
                await ws_connection.send_json({
                    "directive_type": "SHOW_NOTIFICATION",
                    "message": f"Agent sent unhandled response format: {str(envelope.content)[:100]}",
                    "notification_type": "error",
                })
        except Exception as e:
            print(f"[RELAY-WS][ERROR] Failed to send UI Directive to AetherDeck User {user_id} via WebSocket: {e}")

    try:
        await subscribe(redis_client, directive_stream, handler, group=group_name)
    except asyncio.CancelledError:
        print(f"[RELAY-WS][UNSUBSCRIBE] Listener for User {user_id} on {directive_stream} cancelled.")
    except Exception as e:
        print(f"[RELAY-WS][ERROR] Subscriber for User {user_id} on {directive_stream} crashed: {e}")
    finally:
        print(f"[RELAY-WS][STOPPED] Stopped listening on Bus Stream {directive_stream} for User {user_id}")


async def websocket_handler(request: web.Request):
    """Handle incoming WebSocket connections from AetherDeck clients."""
    ws = web.WebSocketResponse(protocols=("json",))
    try:
        await ws.prepare(request)
    except Exception as e:
        print(f"[WS-PREPARE][ERROR] WebSocket preparation failed for {request.remote}: {e}")
        return web.Response(status=400, text=f"WebSocket handshake failed: {e}")

    user_id = request.query.get("user_id")
    session_code = request.query.get("session_code")
    client_api_key = request.query.get("api_key")

    if not user_id:
        user_id = f"aetherdeck_guest_{str(uuid.uuid4())[:8]}"
        print(f"[WS][INFO] No user_id provided by {request.remote}. Assigned default: {user_id}")

    if REQUIRE_WS_API_KEY:
        if not client_api_key or client_api_key != API_KEY:
            await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b"Unauthorized: Invalid or missing API key.")
            return ws

    if user_id in active_websockets:
        try:
            await active_websockets[user_id].close(code=WSCloseCode.GOING_AWAY, message=b"New connection for this user_id.")
        except Exception as e_close_old:
            print(f"[WS][WARN] Error closing old WebSocket for {user_id}: {e_close_old}")
        if user_id in active_websockets:
            del active_websockets[user_id]

    print(f"[WS][CONNECT] AetherDeck client connected: UserID='{user_id}', SessionCode='{session_code}', Remote='{request.remote}'")
    active_websockets[user_id] = ws

    redis_client = request.app['redis_pool']
    bus_listener_task = None

    try:
        initial_chat_window_directive = {
            "directive_type": "CREATE_WINDOW",
            "window_id": "main_chat_window",
            "title": f"AetherDeck Chat ({user_id})",
            "initial_components": [
                {
                    "component_id": "chat_log",
                    "component_type": "text_display",
                    "content": "Chat session started. Type your message below.\n",
                }
            ],
            "position": {"x": 100, "y": 100},
            "size": {"width": 450, "height": 350},
        }
        await ws.send_json(initial_chat_window_directive)
        bus_listener_task = asyncio.create_task(listen_for_user_directives(user_id, ws, redis_client))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == 'close':
                    await ws.close()
                else:
                    await handle_aetherdeck_event(user_id, session_code, msg.data, redis_client, ws)
            elif msg.type == WSMsgType.ERROR:
                print(f'[WS][ERROR] WebSocket connection for User {user_id} closed with exception {ws.exception()}')
            elif msg.type == WSMsgType.CLOSED:
                print(f'[WS][CLOSED] Closed message received for User {user_id}')
                break
    except asyncio.CancelledError:
        print(f"[WS][CANCELLED] WebSocket handler task for User {user_id} was cancelled.")
        raise
    finally:
        if user_id in active_websockets and active_websockets[user_id] is ws:
            del active_websockets[user_id]
        if bus_listener_task and not bus_listener_task.done():
            bus_listener_task.cancel()
            try:
                await bus_listener_task
            except asyncio.CancelledError:
                pass
        if not ws.closed:
            await ws.close()
        print(f"[WS][DISCONNECTED] AetherDeck client UserID='{user_id}' fully disconnected.")
    return ws
