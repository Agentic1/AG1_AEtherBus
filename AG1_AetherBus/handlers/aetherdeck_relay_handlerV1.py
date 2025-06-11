# relay_server.py (Now the AetherDeck Edge Handler)
import aiohttp_cors
from aiohttp import web, WSMsgType, WSCloseCode
import uuid
import asyncio
import random
import json 
from dotenv import load_dotenv
#import redis.asyncio as aioredis # Added Redis import here
import os
import sys
import functools
import argparse # Added for main execution block
from datetime import datetime, timezone  # Add timezone for timezone-aware datetimes
from pathlib import Path 

# Assuming your AG1_AetherBus is in a discoverable path
from AG1_AetherBus.bus import publish_envelope, build_redis_url, subscribe
from AG1_AetherBus.keys import StreamKeyBuilder # Make sure this is correctly importable
from AG1_AetherBus.envelope import Envelope
from redis.asyncio import Redis #as AIORedis # Added AIORedis

# Ensure Redis is imported for type hinting and use
from redis.asyncio import Redis # Explicit import

# This should be the 'BrainProject3' directory in your structure.
# Check if we're in production (where AG1_CoreServices is in the path)
is_production = os.path.exists('/home/ubuntu/AG1Servers/AG1_CoreServices')

if is_production:
    # In production, add the AG1Servers directory to path
    sys.path.insert(0, '/home/ubuntu/AG1Servers')
    from AG1_CoreServices.SessionManager import SessionManager
else:
    # In development, use the local path
    project_root = Path(__file__).parent.parent.parent.parent
    brain_project_root = project_root / "BrainProject3"
    if str(brain_project_root) not in sys.path:
        sys.path.insert(0, str(brain_project_root))
    
from AG1_CoreServices.SessionManager import SessionManager

session_manager: SessionManager | None = None 


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
sessions: dict[str, dict] = {}

# UI Directive Handlers
async def handle_ui_directive(websocket, directive: dict):
    """Route UI directives to the appropriate handler."""
    directive_type = directive.get('directive_type', '').upper()
    
    try:
        if directive_type == 'CREATE_WINDOW':
            return await handle_create_window(websocket, directive)
        elif directive_type == 'APPEND_TO_TEXT_DISPLAY':
            return await handle_append_to_display(websocket, directive)
        else:
            logger.warning(f"Unknown directive type: {directive_type}")
            return {"status": "error", "message": "Unknown directive type"}
    except Exception as e:
        logger.error(f"Error handling UI directive: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}

async def handle_create_window(websocket, directive: dict) -> dict:
    """Handle CREATE_WINDOW directive."""
    required_fields = ['window_id', 'title', 'initial_components']
    if not all(field in directive for field in required_fields):
        return {"status": "error", "message": "Missing required fields for CREATE_WINDOW"}
    
    # Validate chat_view component if present
    for comp in directive.get('initial_components', []):
        if comp.get('component_type') == 'chat_view':
            if not comp.get('component_id'):
                return {"status": "error", "message": "chat_view component must have component_id"}
    
    # Forward to WebSocket
    await websocket.send_json(directive)
    return {"status": "success"}

async def handle_append_to_display(websocket, directive: dict) -> dict:
    """Handle APPEND_TO_TEXT_DISPLAY directive."""
    required_fields = ['window_id', 'component_id', 'content_to_append']
    if not all(field in directive for field in required_fields):
        return {"status": "error", "message": "Missing required fields for APPEND_TO_TEXT_DISPLAY"}
    
    # Ensure content ends with newline
    if not directive['content_to_append'].endswith('\n'):
        directive['content_to_append'] += '\n'
    
    # Forward to WebSocket
    await websocket.send_json(directive)
    return {"status": "success"}

# --- Handle Incoming AetherDeck Registrations (from agents like MuseAgent) ---
async def handle_aetherdeck_registration_envelope(env: Envelope, redis_client: Redis):
    """
    Processes 'register' envelopes specific to AetherDeck,
    updating the internal registry of agents that handle AetherDeck traffic.
    """
    print(f"[AETHERDECK_HANDLER][REG_RECEIVE] Received envelope on registration stream: Type={env.envelope_type}, Agent={env.agent_name}, Content={env.content}") # ADD THIS
    if env.envelope_type == "register" and env.content.get("channel_type") == "aetherdeckv1":
        agent_name = env.agent_name
        aetherdeck_user_id_pattern = env.content.get("aetherdeckv1_user_id_pattern") # This allows registering for "all" users or specific ones
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
    New version: Uses SessionManager instead of forwarding_context.
    Expects browser to send: {
      event_type: "direct_to_agent_input",
      target_agent_inbox: "...",
      text: "...",
      window_id: "...",
      session_id: "...",
      user_id: "..."
    }
    """
    reply_to_stream_for_ui_directives = kb.edge_response("aetherdeckv1", user_id)
    print(f"[RELAY-WS][DEBUG] reply_to_stream_for_ui_directives set to {reply_to_stream_for_ui_directives}")
    global session_manager

    try:
        event_data = json.loads(raw_message_data)
        print(f"xxx--->>[RELAY-WS][EVENT_RAW] User {user_id} Session {session_code} RawData: {raw_message_data[:200]}")

        source_event_type = event_data.get("event_type")

        # --- Echo User Input for ANY textual input from UI before sending to backend ---
        # This applies to both direct_to_agent_input and user_chat_input if they contain text.
        text_input_from_user = event_data.get("text", "").strip()
        source_window_id_for_echo = event_data.get("window_id") # For direct_to_agent_input
        
        if text_input_from_user: # Only echo if there's text
            echo_window_id = "main_chat_window" # Default for user_chat_input
            echo_component_id = "chat_log"       # Default for user_chat_input
            echo_meta_for_directive = {}         # Default for main_chat_window (hits Case 3 in directive_handler)

            if source_event_type == "direct_to_agent_input":
                # For direct_to_agent_input, we need the session to get the correct window and component ID
                # And we want it to be handled by Case 1 of directive_envelope_handler
                session_for_echo = await session_manager.get(event_data.get("session_id")) or \
                                   await session_manager.get_by_window(event_data.get("window_id"))
                if session_for_echo:
                    echo_window_id = session_for_echo.get("window_id", source_window_id_for_echo or "main_chat_window")
                    echo_component_id = session_for_echo.get("default_component_id", "output_area")
                    echo_meta_for_directive = {**session_for_echo} # Pass full session for V1 handling
                    # Ensure is_direct_ui_forward is True if not already, so it uses V1 append logic
                    echo_meta_for_directive["is_direct_ui_forward"] = True 
                else: # Should not happen if direct_to_agent_input is well-formed
                    print(f"[RELAY-WS][USER_ECHO_WARN] No session found for direct_to_agent_input echo. Defaulting to main chat. Event: {event_data}")
            
            user_echo_display_text = f"\nX=You: {text_input_from_user}\n"
            
            # This envelope goes onto the user's AD response stream
            user_echo_envelope = Envelope(
                role="User", 
                agent_name="User",
                content={"text": user_echo_display_text},
                meta=echo_meta_for_directive, 
                user_id=user_id, # The user who typed
                session_code=session_code if source_event_type == "user_chat_input" else event_data.get("session_id"),
                reply_to=reply_to_stream_for_ui_directives,
                envelope_type="ui_input_echo"
            )
            print(f"[RELAY-WS][USER_ECHO] Publishing user input echo to {reply_to_stream_for_ui_directives} for window '{echo_window_id}'")
            print(f"[RELAY-WS][USER_ECHO] Publishing user input echo metaa to {echo_meta_for_directive} ")

            await publish_envelope(redis_client, reply_to_stream_for_ui_directives, user_echo_envelope)
        # --- End User Input Echo ---

        # ──────────────── direct_to_agent_input ────────────────
        if source_event_type == "direct_to_agent_input":
            # 1) Extract the fields we now expect:
            target_specific_agent_inbox = event_data.get("target_agent_inbox")
            text_for_specific_agent     = event_data.get("text", "")
           
            session_id_from_ui          = event_data.get("session_id")
            user_id_from_ui             = event_data.get("user_id")

            window_id   = event_data["window_id"]
            agent_inbox = event_data["target_agent_inbox"]
            text        = event_data["text"]


        
            print(f"---> [RELAY-WS] direct_to_agent_input ➞ User={user_id_from_ui}, Window={window_id}, Session={session_id_from_ui}, TargetInbox={target_specific_agent_inbox}")

            # 2) Lookup session by window_id (or fallback to session_id):
            session = await session_manager.get_by_window(window_id)
            print(f'---->[Relay] session1window {session}')
            if not session and session_id_from_ui:
                session = await session_manager.get(session_id_from_ui)
                succ = f"---->[RELAY-WS] session FOUND for window_id={window_id} and session={session}"
                print(succ)

            if not session:
                err_msg = f"---->[RELAY-WS] No session found for window_id={window_id} or session_id={session_id_from_ui}"
                print(err_msg)
                await ws_connection.send_json({
                    "directive_type": "SHOW_NOTIFICATION",
                    "message": "Error: Session not found. Please reopen the window.",
                    "notification_type": "error"
                })
                return

            
            
            

            # 3) Build the Envelope for HeartBeat (Web only):
            direct_message_content = {
                "text":            text_for_specific_agent, #text_for_agent,
                "source_channel":  "aetherdeckv1_direct",
                "window_id":       session["window_id"]
            }

            reply_to_address_for_backend = session.get("reply_to_for_backend_agent")
            print(f"[handle_aetherdeck_event] Preparing envelope for target: {target_specific_agent_inbox}, reply_to (Muse2): {reply_to_address_for_backend}")

            print(f'[handle_aetherdeck_event] drect_messagemeta has response_stream {direct_message_content}')
            direct_envelope = Envelope(
                role="user",
                user_id=session["user_id"],
                session_code=session["session_id"],
                agent_name="aetherdeckv1_ui_direct_send",
                envelope_type="message",
                content=direct_message_content,
                meta=session,                               #NEW SESSIONBEING PASSED NOW.
                reply_to=reply_to_address_for_backend , #session["response_stream"],  # e.g. "AG1:edge:response:aetherdeck:guest123"
                correlation_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

            )

            # 4) Publish to the agent’s Redis inbox
            print(f"[RELAY-WS] Publishing to {target_specific_agent_inbox}: {direct_envelope}")
            await publish_envelope(redis_client, target_specific_agent_inbox, direct_envelope)
            print(f"[RELAY-WS] Sent direct_to_agent_input from User {user_id_from_ui} → {target_specific_agent_inbox}")
            return

        # ──────────────── Primary agent routing ────────────────
        # (unchanged from your previous code, just copy/paste this block)
        target_primary_agent_info = aetherdeck_registered_agents.get(user_id)
        if not target_primary_agent_info and "all" in aetherdeck_registered_agents:
            target_primary_agent_info = aetherdeck_registered_agents.get("all")

        if not target_primary_agent_info:
            err_msg = f"No primary agent registered for event '{source_event_type}' from user '{user_id}'."
            print(f"[RELAY-WS][ERROR] {err_msg}")
            await ws_connection.send_json({
                "directive_type": "SHOW_NOTIFICATION",
                "message": err_msg,
                "notification_type": "error"
            })
            return

        target_primary_agent_inbox_stream = target_primary_agent_info["agent_inbox_stream"]
        print(f"[RELAY-WS][ROUTING_PRIMARY] Event '{source_event_type}' for User {user_id} to primary agent: {target_primary_agent_inbox_stream}")

        # Normalize other event types (user_chat_input, component_interaction, ui_event, etc.)
        text_for_primary = ""
        #XXXXXXXXX
        print(f"[RELAY-WS][USER_ECHO_LOGIC] Publishing User Echo Envelope to {reply_to_stream_for_ui_directives}. Raw: {user_echo_envelope.to_dict() if hasattr(user_echo_envelope, 'to_dict') else vars(user_echo_envelope)}")
        if source_event_type == "user_chat_input":
            text_for_primary = event_data.get("text", "").strip()
            """ # --- START: User Input Echo for user_chat_input (main window) ---
                if text_for_primary:
                user_echo_text = f"You: {text_for_primary}\n"
                # For main chat, meta should be minimal so Relay's directive_handler uses default append
                main_chat_echo_meta = {"source": "user_echo_main_chat"} # Minimal meta
                user_echo_envelope = Envelope(
                    role="agent", agent_name="User", # Or "You"
                    content={"text": user_echo_text}, meta=main_chat_echo_meta,
                    user_id=user_id, session_code=session_code,
                    reply_to=reply_to_stream_for_ui_directives, envelope_type="ui_input_echo"
                )
                print(f"[RELAY-WS][USER_ECHO_MAIN] Publishing user input echo to {reply_to_stream_for_ui_directives} for main_chat_window")
            
                await publish_envelope(redis_client, reply_to_stream_for_ui_directives, user_echo_envelope)"""
            # --- END: User Input Echo for user_chat_input ---
            print(f"[RELAY-WS][NORMALIZED] User chat input for primary: '{text_for_primary[:100]}...'")

        elif source_event_type == "component_interaction":
            component_id = event_data.get("component_id", "N/A")
            event_name = event_data.get("event_name", "N/A")
            payload_data = event_data.get("payload", {})
            text_for_primary = f"User interacted (event:'{event_name}', component:'{component_id}'). Payload: {json.dumps(payload_data)}"
            print(f"[RELAY-WS][NORMALIZED] Component interaction: '{text_for_primary[:100]}...'")
        elif source_event_type == "ui_event":
            action = event_data.get("action", "N/A")
            w_id = event_data.get("window_id", "N/A")
            text_for_primary = f"UI event: '{action}' for Window ID: '{w_id}'."
            print(f"[RELAY-WS][NORMALIZED] UI event: '{text_for_primary[:100]}...'")
        else:
            text_for_primary = f"[AetherDeck Event (to primary): {source_event_type}] Raw: {json.dumps(event_data)}"
            print(f"[RELAY-WS][NORMALIZED] Unknown event: '{text_for_primary[:100]}...'")

        primary_agent_envelope_content = {
            "text": text_for_primary,
            "source_channel": "aetherdeckv1",
            "original_event_type": source_event_type,
            "full_event_data": event_data
        }
        if event_data.get("source_window_id"):
            primary_agent_envelope_content["source_window_id"] = event_data.get("source_window_id")

        primary_agent_envelope = Envelope(
            role="user_interface_event",
            user_id=user_id,
            session_code=session_code,
            reply_to=reply_to_stream_for_ui_directives,
            content=primary_agent_envelope_content,
            agent_name="aetherdeckv1_relay_ws",
            envelope_type="ui_event",
            correlation_id=str(uuid.uuid4())
        )

        await publish_envelope(redis_client, target_primary_agent_inbox_stream, primary_agent_envelope)
        print(f"[RELAY-WS][PUBLISHED_PRIMARY] Sent event from User {user_id} → Primary Agent Inbox: {target_primary_agent_inbox_stream}")

    except json.JSONDecodeError:
        err_msg = f"Invalid JSON received from AetherDeck client {user_id}"
        print(f"[RELAY-WS][ERROR] {err_msg}: {raw_message_data}")
        try:
            await ws_connection.send_json({"error": "Invalid JSON", "type": "protocol_error"})
        except Exception as e_send:
            print(f"[RELAY-WS][ERROR] Failed to send JSON error to client {user_id}: {e_send}")

    except Exception as e:
        err_msg = f"Error handling AetherDeck event from {user_id}"
        print(f"[RELAY-WS][ERROR] {err_msg}: {e}")
        try:
            await ws_connection.send_json({"error": str(e), "type": "server_error"})
        except Exception as e_send:
            print(f"[RELAY-WS][ERROR] Failed to send server error to client {user_id}: {e_send}")




async def listen_for_user_directives(user_id, ws_connection, redis_client):
    """
    Listens on the AetherBus for UI Directives targeted at a specific AetherDeck user
    and sends them over the WebSocket connection.
    """
    directive_stream = kb.edge_response("aetherdeckv1", user_id) # USE edge_response pattern
    print(f"[RELAY-WS][SUBSCRIBE] Listening on Bus Stream {directive_stream} for AetherDeck directives for User {user_id}")

    # Use a unique group name for each user's listener to prevent message duplication
    group_name = f"aetherdeckv1_ws_listener_{user_id}"
    try:
        await redis_client.xgroup_create(directive_stream, group_name, mkstream=True, id='$') # Ensure group exists
    except Exception as e:
        # This can fail if group already exists, which is fine. Log other errors.
        if "BUSYGROUP" not in str(e):
            print(f"[RELAY-WS][XGROUP_CREATE][WARN] Failed to create XGroup for {directive_stream} with group {group_name}: {e}")


    
    async def directive_envelope_handler(envelope: Envelope):
        global session_manager # Ensure session_manager is accessible
        # 'user_id' is available from the outer scope of listen_for_user_directives

        print(f"[RELAY-WS][DIRECTIVE_ENTRY] User {user_id}, EnvelopeID: {envelope.envelope_id}, Agent: {envelope.agent_name}")
        # For brevity in logs, maybe just print meta keys or specific fields
        if isinstance(envelope.meta, dict):
            print(f"  Meta keys: {list(envelope.meta.keys())}, session_id: {envelope.meta.get('session_id')}, window_id_in_meta: {envelope.meta.get('window_id')}")
        else:
            print(f"  Meta: {str(envelope.meta)[:100]}")
        print(f"  Content: {str(envelope.content)[:150]}")


        if ws_connection.closed:
            print(f"[RELAY-WS][WARN] WebSocket for User {user_id} is closed.")
            return

        try:
            # --- Case 1: V1 Forwarded Message for a specific session ---
            if isinstance(envelope.meta, dict) and envelope.meta.get("is_direct_ui_forward") is True:
                session_record_from_message_meta = envelope.meta # This is the record forwarded by Muse2
                session_id = session_record_from_message_meta.get("session_id")
                user_id_from_session = session_record_from_message_meta.get("user_id") # Should match current 'user_id'

                #-- wheree here ??

                if not session_id or not user_id_from_session:
                    print(f"[RELAY-WS][ERROR] V1 forwarded message missing session_id or user_id in meta: {session_record_from_message_meta}")
                    return # Or send error to UI

                # Get the authoritative session record from SessionManager
                session_record_from_store = await session_manager.get(session_id)

                window_id_to_use = None
                component_id_to_use = session_record_from_message_meta.get("default_component_id", "output_area") # Get from incoming meta

                if session_record_from_store and session_record_from_store.get("window_id"):
                    # Window ID already exists in SessionManager for this session
                    window_id_to_use = session_record_from_store.get("window_id")
                    print(f"[RELAY-WS][EXISTING_WINDOW] Session '{session_id}' already has window_id '{window_id_to_use}' in store.")

                    # IMPORTANT: Update SessionManager with potentially newer info from the message's meta
                    # (e.g., EchoAgent updated target_backend_agent_inbox)
                    # Merge: Start with store, overlay with message_meta, ensure window_id is the stored one.
                    updated_record_data = {**session_record_from_store, **session_record_from_message_meta}
                    updated_record_data["window_id"] = window_id_to_use # Preserve existing window_id
                    
                    # Only re-save if there were actual changes relevant to SessionManager's schema
                    # For now, let's assume create_session handles updates efficiently.
                    await session_manager.create_session(**updated_record_data) # create_session can act as update
                    print(f"[RELAY-WS][SESSION_REFRESH] Refreshed session '{session_id}' in store with latest meta.")

                else:
                    # New window scenario: No window_id in SessionManager for this session_id,
                    # or session_record_from_store was None (Muse created it, but Relay hasn't processed it yet).
                    print(f"[RELAY-WS][NEW_WINDOW] No window_id in store for session '{session_id}'. Creating new window.")
                    
                    conceptual_name = session_record_from_message_meta.get('agent_name_conceptual', 'AgentView')
                    actual_new_window_id = f"ad_win_{conceptual_name.lower().replace(' ','_')}_{str(uuid.uuid4())[:8]}"

                    component_id_to_use = session_record_from_message_meta.get("default_component_id", "output_area") # From Muse tool
                
                    # --- START: Custom Size/Position for Gatekeeper ---
                    window_size = {"width": 750, "height": 450} # Default size
                    window_position = {"x": 150, "y": 150} # Default position (maybe randomise slightly)

                    # Define initial components
                    initial_components_for_window = [
                        {
                            "component_id": component_id_to_use, 
                            "component_type": "text_display", 
                            "content": f"Window for {conceptual_name} started.\n" 
                        }
                    ]

                    if conceptual_name == "NameCapture": # Or your specific conceptual name for Gatekeeper
                        window_size = {"width": 450, "height": 400} # Make it wider, less tall
                        # Center it? (Requires knowing viewport size, harder for Relay)
                        # For now, just a fixed prominent position:
                        window_position = {"x": (1920//2 - 450//2), "y": (1080//2 - 200//2 - 100)} 
                        print(f"[RELAY-WS][GATEKEEPER_SETUP] Setting custom size/pos and adding input field for Gatekeeper window.")
                        
                        # Add the dedicated input field for Gatekeeper
                        initial_components_for_window.append({
                            "component_id": "gatekeeper_name_input_field", # A fixed ID for this input
                            "component_type": "input",
                            "label": "Your Name:",
                            "placeholder": "Enter your name and press Enter",
                            "value": "",
                            "subscribes_to_global_input": False # This input is specific to this window
                        })
                        
                        print(f"[RELAY-WS][GATEKEEPER_SIZE] Setting custom size/pos for Gatekeeper window.")
                    # --- END: Custom Size/Position ---
                    
                    # 1. Update SessionManager with the new window_id AND the full current session_record_from_message_meta
                    # This ensures the dynamic inbox from Echo, etc., is stored.
                    record_to_store_for_new_window = {**session_record_from_message_meta} # Make a copy
                    record_to_store_for_new_window["window_id"] = actual_new_window_id
                    await session_manager.create_session(**record_to_store_for_new_window) # create_session will store it
                    print(f"[RELAY-WS][SESSION_NEW_WINDOW] Stored session '{session_id}' with new window_id '{actual_new_window_id}'.")

                    # 2. Send CREATE_WINDOW to UI
                    target_inbox_for_ui = record_to_store_for_new_window.get("actual_echo_agent_inbox") or \
                                        record_to_store_for_new_window.get("target_backend_agent_inbox")
                    
                    create_window_directive = {
                        "directive_type": "CREATE_WINDOW",
                        "window_id": actual_new_window_id,
                        "title": conceptual_name,
                        "size": window_size,            
                        "position": window_position,    
                        "initial_components": initial_components_for_window,
                        "window_metadata": {
                            "user_id": user_id_from_session,
                            "session_id": session_id,
                            "target_agent_inbox": target_inbox_for_ui,
                            "agent_inbox": target_inbox_for_ui # For current frontend
                        }
                    }
                    print(f"--->[RELAY-WS][CREATE_WINDOW_DEBUG] Creating Gatekeeper window. Metadata being sent: {create_window_directive.get('window_metadata')}")

                    await ws_connection.send_json(create_window_directive)
                    print(f"[RELAY-WS][SENT_CREATE_WINDOW] For session '{session_id}', new window '{actual_new_window_id}'.")
                    window_id_to_use = actual_new_window_id

                # Now handle the content of the V1 forwarded message:
                if isinstance(envelope.content, dict) and "directive_type" in envelope.content:
                    # The content itself is a directive (e.g., CLOSE_WINDOW from Gatekeeper)
                    directive_to_send_to_ui = envelope.content
                    print(f"[RELAY-WS][V1_FORWARDED_DIRECTIVE] Processing V1 forwarded directive: {directive_to_send_to_ui.get('directive_type')} for window {directive_to_send_to_ui.get('window_id')}")
                    # Optional: Add sanity check if directive_to_send_to_ui.get("window_id") matches window_id_to_use for CLOSE_WINDOW
                    await ws_connection.send_json(directive_to_send_to_ui) # Send the directive as is
                
                elif isinstance(envelope.content, dict) and "text" in envelope.content:
                    # The content is text to be appended to the V1 window
                    text_to_display = envelope.content["text"]
                    await ws_connection.send_json({
                        "directive_type": "APPEND_TO_TEXT_DISPLAY",
                        "window_id": window_id_to_use,
                        "component_id": component_id_to_use, 
                        "content_to_append": text_to_display
                    })
                    print(f"[RELAY-WS][APPEND_V1_WINDOW] To '{window_id_to_use}/{component_id_to_use}': {text_to_display[:50]}…")
                else:
                    print(f"[RELAY-WS][WARN] V1 forwarded message for session '{session_id}' had unhandled content structure: {envelope.content}")
                # --- END OF FIX for content handling within Case 1 ---
                return # V1 message processed

            # --- Case 2: Message content IS ALREADY a UI directive (e.g., initial main chat CREATE_WINDOW) ---
            elif isinstance(envelope.content, dict) and "directive_type" in envelope.content:
                """await ws_connection.send_json(envelope.content)
                print(f"[RELAY-WS][SENT_PREFORMATTED_DIRECTIVE] Type: {envelope.content.get('directive_type')}")
                return # Directive processed
                """
                directive_to_send = envelope.content
                # Sanity check: if it's a CLOSE_WINDOW, ensure it's for the current context's window
                if directive_to_send.get("directive_type") == "CLOSE_WINDOW" and \
                   directive_to_send.get("window_id") != window_id_to_use:
                    print(f"[RELAY-WS][WARN] CLOSE_WINDOW directive in V1 message targets '{directive_to_send.get('window_id')}' but current V1 window context is '{window_id_to_use}'. Sending as is.")
                
                print(f"[RELAY-WS][V1_FORWARDED_DIRECTIVE] Processing V1 forwarded directive: {directive_to_send.get('directive_type')} for window {directive_to_send.get('window_id')}")
                await ws_connection.send_json(directive_to_send) # Send the directive as is


            # --- Case 3: Default - General message (likely from Muse2's LLM for main chat) ---
            elif isinstance(envelope.content, dict) and "text" in envelope.content:
                text_to_display = envelope.content["text"]
                # This path is for messages not part of a V1 direct_ui_forward flow,
                # typically replies from Muse2's main LLM to the user's main chat.
                # Meta here is usually empty or from the original user request to Muse2.
                print(f"[RELAY-WS][DEFAULT_APPEND_MAIN] To main_chat_window/chat_log. Meta was: {envelope.meta}")
                await ws_connection.send_json({
                    "directive_type": "APPEND_TO_TEXT_DISPLAY",
                    "window_id": "main_chat_window",
                    "component_id": "chat_log",
                    "content_to_append": text_to_display
                })
                print(f"[RELAY-WS][SENT_TEXT_MAIN_CHAT] Appended: {text_to_display[:50]}…")
                return # Default append processed
                
            # --- Case 4: Unhandled format ---
            else:
                print(f"[RELAY-WS][WARN] Unhandled content format: {envelope.content}")
                await ws_connection.send_json({
                    "directive_type": "SHOW_NOTIFICATION",
                    "message": f"Relay received unhandled agent response format.",
                    "notification_type": "warning"
                })
            
            return #XXXX <--- this correct???

        except Exception as e:
            print(f"[RELAY-WS][ERROR_DIRECTIVE_HANDLER] User {user_id}: {e}")
            import traceback
            traceback.print_exc()
            try:
                await ws_connection.send_json({"directive_type": "SHOW_NOTIFICATION", "message": f"Error: {str(e)}", "notification_type": "error"})
            except Exception: pass # Ignore if can't send error


    try:
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
    
    user_id_from_query = request.query.get("user_id")
    actual_user_id_for_session = None
    is_new_user_id_assigned = False

    if user_id_from_query:
        actual_user_id_for_session = user_id_from_query
        print(f"[WS] User connected with existing ID: {actual_user_id_for_session}")
    else:
        from coolname import generate_slug

        #new_persistent_id = f"H.{str(uuid.uuid4())}" # Mimicking HID format
        fluffy_part = generate_slug(2).replace('-', '') 
        new_persistent_id = f"Usr{fluffy_part.capitalize()}{random.randint(100,999)}"

        actual_user_id_for_session = new_persistent_id
        is_new_user_id_assigned = True # Flag to send SET_USER_ID
        print(f"[WS] New user. Assigned persistent-style ID: {actual_user_id_for_session}")

    # Use actual_user_id_for_session for active_websockets, stream names, etc.
    user_id = actual_user_id_for_session # Use this variable name consistently


    ws = web.WebSocketResponse(protocols=('json',)) 
    try:
        await ws.prepare(request)
    except Exception as e:
        print(f"[WS-PREPARE][ERROR] WebSocket preparation failed for {request.remote}: {e}")
        return web.Response(status=400, text=f"WebSocket handshake failed: {e}")

    print(f"[WS] Sent Check new user assigned'{is_new_user_id_assigned}' boolean")
    if is_new_user_id_assigned:
        try:
            await ws.send_json({
                "directive_type": "SET_USER_ID",
                "user_id": actual_user_id_for_session
            })
            print(f"[WS] Sent SET_USER_ID '{actual_user_id_for_session}' to new client.")
        except Exception as e_set_id:
            print(f"[WS][ERROR] Failed to send SET_USER_ID: {e_set_id}")
            
    # --- User/Session Identification & Authorization ---
    #user_id = request.query.get("user_id")
    session_code = request.query.get("session_code") # Optional
    client_api_key = request.query.get("api_key")

    if not user_id:
        # Generate a default user_id if not provided by the client, for unauthenticated use
        user_id = f"aetherdeckv1_guest_{str(uuid.uuid4())[:8]}" 
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
            "title": f"AetherDeck V1 Chat ({user_id})", # Dynamic title
            "initial_components": [
                {
                    "component_id": "chat_log",
                    "component_type": "chat_view",
                    "content": "Chat session started. Type your message below.\n"
                }
            ],
            "position": {"x": 200, "y": 100},
            "size": {"width": 650, "height": 450}
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
                    print(f'NB: WS Handler -- > msg data ! ', msg.data)
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
    app['redis_pool'] = await Redis.from_url(REDIS_URL, decode_responses=True)
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

        global session_manager
        redis_url = build_redis_url()
        print(f"--->[WebRelay] Connecting to Redis: {redis_url}")
        redis_client_session = await Redis.from_url(redis_url, decode_responses=False)
        print("[WebRelay] Redis client connected.")

        session_manager = SessionManager(redis_client_session)   # no argument
        print("----->[WebRelay] Session Manager started")


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
        registration_patterns = [kb.edge_register("aetherdeckv1")] # Using existing pattern
        print(f"[AETHERDECK_HANDLER][INIT] Subscribing to agent registration stream: {registration_patterns}")
        
        # CORRECTED: Pass redis_client, stream_name, and handler positionally
        for stream_pattern in registration_patterns:
            asyncio.create_task(
                subscribe( 
                    redis_for_subscriptions, # Positional arg 1
                    stream_pattern,          # Positional arg 2
                    functools.partial(handle_aetherdeck_registration_envelope, redis_client=redis_for_subscriptions), # Positional arg 3
                    group="aetherdeckv1_registration_listener" # Keyword arg
                )
            )
        
        # Give a moment for registration listener to set up (optional, just for cleaner logs)
        await asyncio.sleep(0.5) 
        print("[AETHERDECK_HANDLER][INIT] Agent registration listener started.")

        # Start the aiohttp web application (this will now be the main blocking call)
        listen_port = int(os.getenv("RELAY_PORT", 4002))
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