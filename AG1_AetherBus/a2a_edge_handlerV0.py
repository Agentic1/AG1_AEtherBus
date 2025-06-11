#!/usr/bin/env python3
"""
A2A Edge Handler - Subscriber-based Edge Node for Agent-to-Agent Protocol

This module implements a subscriber-based edge node for the Google Agent-to-Agent (A2A) protocol,
allowing agents in the bus architecture to communicate with external A2A-compatible agents.

Usage:
    python a2a_edge_handler.py

Environment Variables:
    REDIS_HOST: Redis host (from AG1_AetherBus.bus)
    REDIS_PORT: Redis port (from AG1_AetherBus.bus)
    REDIS_USERNAME: Redis username (optional)
    REDIS_PASSWORD: Redis password (optional)
"""

import asyncio
import json
import uuid
import os
import datetime
import traceback
from typing import Dict, Any, Optional, List, Tuple
import aiohttp
from dotenv import load_dotenv
import redis.asyncio as aioredis

# Load environment variables
load_dotenv()

# Import bus utilities
try:
    from AG1_AetherBus.bus import subscribe, publish_envelope, build_redis_url
    from AG1_AetherBus.envelope import Envelope
    from AG1_AetherBus.keys import StreamKeyBuilder
    from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
    from bus_adapterV2 import BusAdapterV2
except ImportError:
    print("[a2a_edge] ERROR: Please install the AG1_AetherBus package and ensure bus_adapterV2.py is available")
    exit(1)

# --- Constants ---
# Initialize key builder
keys = StreamKeyBuilder()

# Channel names using StreamKeyBuilder
REGISTER_CHANNEL = keys.a2a_register()  # "AG1:a2a:register"
INBOX_CHANNEL = keys.a2a_inbox("edge")  # "AG1:a2a:agent:edge:inbox"

# --- Agent Registry ---
registered_agents = {}

# --- Active Streaming Tasks ---
streaming_tasks = {}

# --- Stream Key Builder ---
keys = StreamKeyBuilder()

# --- Helper Functions ---

def make_json_safe(obj: Any) -> Any:
    """Convert objects to JSON-serializable format"""
    if hasattr(obj, "to_dict"):
        return make_json_safe(obj.to_dict())
    elif isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    elif hasattr(obj, "__dict__"):
        # For simple objects, only include non-private, non-callable attributes
        return {k: make_json_safe(v) for k, v in vars(obj).items() 
                if not k.startswith('_') and not callable(v)}
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        # For other types, convert to string as fallback
        try:
            json.dumps(obj)  # Test if directly serializable
            return obj
        except TypeError:
            return str(obj)

# --- A2A Protocol Functions ---

async def call_a2a_endpoint(
    endpoint: str, 
    method: str, 
    params: Dict[str, Any], 
    auth_type: Optional[str] = None, 
    auth_key: Optional[str] = None
) -> Dict[str, Any]:
    """Call an A2A endpoint with the specified method and parameters"""
    # Prepare JSON-RPC 2.0 request
    request_payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": str(uuid.uuid4())
    }
    
    # Prepare headers
    headers = {
        "Content-Type": "application/json"
    }
    
    # Add authentication if provided
    if auth_type and auth_key:
        if auth_type.lower() == "bearer":
            headers["Authorization"] = f"Bearer {auth_key}"
        elif auth_type.lower() == "apikey":
            headers["X-API-Key"] = auth_key
        # Add other auth types as needed
    
    print(f"[a2a_edge] Calling A2A endpoint: {endpoint}, method: {method}")
    print(f"[a2a_edge] Request payload: {json.dumps(request_payload)}")
    
    # Set timeout for the request
    timeout = aiohttp.ClientTimeout(total=60)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, json=request_payload, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"HTTP error {response.status}: {error_text}")
                
                result = await response.json()
                print(f"[a2a_edge] Response: {json.dumps(result)}")
                
                # Check for JSON-RPC error
                if "error" in result:
                    raise Exception(f"JSON-RPC error: {result['error']}")
                
                return result
    except aiohttp.ClientError as e:
        raise Exception(f"HTTP client error: {str(e)}")
    except Exception as e:
        raise Exception(f"Error calling A2A endpoint: {str(e)}")

async def handle_streaming_response(
    endpoint: str,
    method: str,
    params: Dict[str, Any],
    redis_client,
    reply_to: str,
    correlation_id: str,
    auth_type: Optional[str] = None,
    auth_key: Optional[str] = None,
    task_id: str = None
) -> None:
    """Handle streaming responses from A2A endpoints using Server-Sent Events"""
    # For streaming methods like tasks/sendSubscribe
    if not method.endswith("Subscribe"):
        raise ValueError(f"Method {method} is not a streaming method")
    
    # Prepare JSON-RPC 2.0 request
    request_payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": str(uuid.uuid4())
    }
    
    # Prepare headers
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }
    
    # Add authentication if provided
    if auth_type and auth_key:
        if auth_type.lower() == "bearer":
            headers["Authorization"] = f"Bearer {auth_key}"
        elif auth_type.lower() == "apikey":
            headers["X-API-Key"] = auth_key
    
    print(f"[a2a_edge] Starting streaming request to {endpoint}, method: {method}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=request_payload, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"HTTP error {response.status}: {error_text}")
                
                # Process SSE stream
                buffer = ""
                async for line in response.content:
                    line = line.decode('utf-8')
                    buffer += line
                    
                    # Check if we have a complete SSE event
                    if buffer.endswith('\n\n'):
                        # Parse SSE event
                        event_data = None
                        for part in buffer.split('\n'):
                            if part.startswith('data: '):
                                event_data = part[6:]  # Remove 'data: ' prefix
                        
                        if event_data:
                            try:
                                json_data = json.loads(event_data)
                                
                                # Create envelope for the streaming update
                                stream_env = Envelope(
                                    role="agent",
                                    content={"result": json_data},
                                    correlation_id=correlation_id,
                                    agent_name="a2a_edge",
                                    envelope_type="stream_update",
                                    meta={"task_id": task_id} if task_id else None
                                )
                                
                                # Publish to reply channel
                                await publish_envelope(redis_client, reply_to, stream_env)
                                print(f"[a2a_edge] Published streaming update to {reply_to}")
                            except json.JSONDecodeError:
                                print(f"[a2a_edge] Error parsing SSE event data: {event_data}")
                        
                        # Reset buffer for next event
                        buffer = ""
    except Exception as e:
        # Send error notification
        error_env = Envelope(
            role="system",
            content={"error": f"Streaming error: {str(e)}"},
            correlation_id=correlation_id,
            agent_name="a2a_edge",
            envelope_type="error",
            meta={"task_id": task_id} if task_id else None
        )
        await publish_envelope(redis_client, reply_to, error_env)
        print(f"[a2a_edge] Error in streaming: {str(e)}")
        print(traceback.format_exc())

# --- Handler Functions ---

async def handle_registration(env: Envelope, redis_client) -> None:
    """Handle agent registration with the A2A edge"""
    print(f"[a2a_edge] Registration received: {env}")
    
    if not env.agent_name:
        print("[a2a_edge] ERROR: Missing agent_name in registration envelope")
        return
        
    content = env.content or {}
    agent_name = env.agent_name
    a2a_endpoint = content.get("a2a_endpoint")
    auth_type = content.get("auth_type")
    auth_key = content.get("auth_key")
    
    if not a2a_endpoint:
        print(f"[a2a_edge] ERROR: Missing a2a_endpoint in registration for {agent_name}")
        return
    
    # Register the agent
    registered_agents[agent_name] = {
        "name": agent_name,
        "endpoint": a2a_endpoint,
        "auth_type": auth_type,
        "auth_key": auth_key,
        "timestamp": datetime.datetime.now().isoformat()
    }
    
    print(f"[a2a_edge] Registered agent {agent_name} with A2A endpoint {a2a_endpoint}")
    
    # Send confirmation back to the agent using the agent's response channel
    response_channel = keys.a2a_response(agent_name, "registration")
    confirmation_env = Envelope(
        role="system",
        content={"status": "registered", "message": "Successfully registered with A2A edge"},
        agent_name="a2a_edge",
        envelope_type="registration_confirmation",
        timestamp=datetime.datetime.now().isoformat(),
        correlation_id=getattr(env, 'correlation_id', None)
    )  
    
    await publish_envelope(redis_client, response_channel, confirmation_env)
    print(f"[a2a_edge] Sent registration confirmation to {response_channel}")

async def handle_a2a_request(env: Envelope, redis_client) -> None:
    """Process an incoming envelope and forward it to the appropriate A2A endpoint"""
    print(f"[a2a_edge] Received request envelope:.... {env.envelope_id}")
    
    content = env.content or {}
    method = content.get("method")
    params = content.get("params", {})
    a2a_endpoint = content.get("a2a_endpoint")
    auth_type = content.get("auth_type")
    auth_key = content.get("auth_key")
    target_a2a_agent_name = content.get("target_a2a_agent") # e.g., "BraveSearchAgent" or "GoogleMapsA2A"
    print(f"[a2a_edge][DEBUG] Extracted 'target_a2a_agent': '{target_a2a_agent_name}' from envelope content.")
    print(f"[a2a_edge][DEBUG] Current registered_agents keys: {list(registered_agents.keys())}")


    
    # Get reply channel
    reply_to = env.reply_to
    if not reply_to:
        # If no reply_to specified, use edge format
        agent_name = env.agent_name or "unknown"
        reply_to = f"{EDGE_PREFIX}{agent_name}:response"
    
    # Check if we have all required fields
    if not a2a_endpoint and target_a2a_agent_name in registered_agents:
        agent_info = registered_agents[target_a2a_agent_name] # Use target_a2a_agent_name
        #a2a_endpoint = agent_info["endpoint"]
        a2a_endpoint = agent_info.get("endpoint", a2a_endpoint)
        auth_type = auth_type or agent_info.get("auth_type")
        auth_key = auth_key or agent_info.get("auth_key")
        print(f"[a2a_edge] Found registered endpoint for '{target_a2a_agent_name}': {a2a_endpoint}")
    elif not a2a_endpoint: # If still no endpoint and no target_a2a_agent_name found
        error_msg = f"Missing 'a2a_endpoint' in envelope content and target A2A agent '{target_a2a_agent_name}' not registered."
        print(f"[a2a_edge] ERROR: {error_msg}")
        error_env = Envelope(
            role="system",
            content={"error": error_msg},
            correlation_id=env.correlation_id,
            agent_name="a2a_edge",
            envelope_type="error"
        )
        await publish_envelope(redis_client, reply_to, error_env)
        return
    
    # If a2a_endpoint is not provided, check if agent is registered
    if not a2a_endpoint and env.agent_name in registered_agents:
        agent_info = registered_agents[env.agent_name]
        a2a_endpoint = agent_info["endpoint"]
        auth_type = auth_type or agent_info.get("auth_type")
        auth_key = auth_key or agent_info.get("auth_key")
    
    if not a2a_endpoint:
        error_msg = "Missing 'a2a_endpoint' in envelope content and agent not registered"
        print(f"[a2a_edge] ERROR: {error_msg}")
        error_env = Envelope(
            role="system",
            content={"error": error_msg},
            correlation_id=env.correlation_id,
            agent_name="a2a_edge",
            envelope_type="error"
        )
        await publish_envelope(redis_client, reply_to, error_env)
        return
    
    try:
        # Extract task_id if available
        task_id = None
        if params and isinstance(params, dict) and "id" in params:
            task_id = params["id"]
        
        # Check if this is a streaming method
        is_streaming = method.endswith("Subscribe")
        
        if is_streaming:
            # Generate a unique task ID if not provided
            if not task_id:
                task_id = str(uuid.uuid4())
                if isinstance(params, dict):
                    params["id"] = task_id
            
            # Create a unique key for this streaming task using StreamKeyBuilder
            stream_key = keys.a2a_stream(env.agent_name, task_id)  # "AG1:a2a:stream:{agent_name}:{task_id}"
            
            # Cancel any existing streaming task with the same key
            if stream_key in streaming_tasks:
                streaming_tasks[stream_key].cancel()
                print(f"[a2a_edge] Cancelled existing streaming task: {stream_key}")
            
            # Handle streaming in a separate task
            streaming_task = asyncio.create_task(
                 handle_streaming_response(
                    a2a_endpoint, 
                    method, 
                    params, 
                    redis_client,  # Moved up
                    reply_to, 
                    env.correlation_id,
                    auth_type=auth_type,  # Now using named parameters
                    auth_key=auth_key,
                    task_id=task_id
                )
            )
            
            # Store the task for potential cancellation
            streaming_tasks[stream_key] = streaming_task
            
            # Send initial acknowledgment to the agent's response channel
            ack_env = Envelope(
                role="system",
                content={"status": "streaming_started", "task_id": task_id},
                correlation_id=env.correlation_id,
                agent_name="a2a_edge",
                envelope_type="streaming_ack"
            )
            response_channel = keys.a2a_response(env.agent_name, task_id)
            await publish_envelope(redis_client, response_channel, ack_env)
            
            print(f"[a2a_edge] Started streaming task: {stream_key}")
        else:
            # Handle regular (non-streaming) request
            result = await call_a2a_endpoint(a2a_endpoint, method, params, auth_type, auth_key)
            
            # Create response envelope
            response_env = Envelope(
                role="agent", # Or "service" if more appropriate for the A2A response
                content={"result": result},
                correlation_id=env.correlation_id, # Keep the original correlation_id
                agent_name="a2a_edge", # This is the a2a_edge_handler sending the reply
                envelope_type="a2a_response", # Specific type for A2A replies
                meta={"task_id": task_id} if task_id else None,
                user_id=env.user_id, # Pass back original user_id
                session_code=env.session_code # Pass back original session_code
            )
            
            # CORRECTED: Publish the response to the 'reply_to' stream provided in the incoming 'env'
            # This 'env.reply_to' was set by A2AProxy for the RPC call.
            await publish_envelope(redis_client, env.reply_to, response_env)
            print(f"[a2a_edge] Published response to designated reply_to stream: {env.reply_to}")

            # Publish response to the agent's response channel
            #response_channel = keys.a2a_response(env.agent_name, task_id or "")
            #await publish_envelope(redis_client, response_channel, response_env)
            #print(f"[a2a_edge] Published response to {reply_to}")
    
    except Exception as e:
        print(f"[a2a_edge] ERROR processing envelope: {str(e)}")
        print(traceback.format_exc())
        
        # Send error response to the agent's response channel
        error_env = Envelope(
            role="system",
            content={"error": str(e)},
            correlation_id=env.correlation_id,
            agent_name="a2a_edge",
            envelope_type="error",
            meta={"task_id": task_id} if 'task_id' in locals() and task_id else None
        )
        response_channel = keys.a2a_response(env.agent_name, task_id if 'task_id' in locals() else "")
        await publish_envelope(redis_client, response_channel, error_env)

async def handle_task_cancel(env: Envelope, redis_client) -> None:
    """Handle task cancellation requests"""
    print(f"[a2a_edge] Received task cancellation request: {env}")
    
    content = env.content or {}
    task_id = content.get("task_id")
    agent_name = env.agent_name
    
    # Get reply channel using the agent's response channel
    reply_to = env.reply_to or keys.a2a_response(agent_name, "")
    
    if not task_id or not agent_name:
        error_msg = "Missing task_id or agent_name in cancellation request"
        print(f"[a2a_edge] ERROR: {error_msg}")
        error_env = Envelope(
            role="system",
            content={"error": error_msg},
            correlation_id=env.correlation_id,
            agent_name="a2a_edge",
            envelope_type="error"
        )
        await publish_envelope(redis_client, reply_to, error_env)
        return
    
    # Create a unique key for this streaming task
    stream_key = keys.a2a_stream(env.agent_name, task_id)  # "AG1:a2a:stream:{agent_name}:{task_id}"
    
    # Cancel the streaming task if it exists
    if stream_key in streaming_tasks:
        streaming_tasks[stream_key].cancel()
        del streaming_tasks[stream_key]
        print(f"[a2a_edge] Cancelled streaming task: {stream_key}")
        
        # Send cancellation confirmation
        cancel_env = Envelope(
            role="system",
            content={"status": "cancelled", "task_id": task_id},
            correlation_id=env.correlation_id,
            agent_name="a2a_edge",
            envelope_type="task_cancelled"
        )
        # Send cancellation confirmation to the agent's response channel
        response_channel = keys.a2a_response(agent_name, task_id)
        await publish_envelope(redis_client, response_channel, cancel_env)
    else:
        # Task not found - send error to agent's response channel
        not_found_env = Envelope(
            role="system",
            content={"status": "not_found", "task_id": task_id, "error": f"Task {task_id} not found"},
            correlation_id=env.correlation_id,
            agent_name="a2a_edge",
            envelope_type="task_not_found"
        )
        await publish_envelope(redis_client, reply_to, not_found_env)

# --- Reporter Task ---
async def reporter_task(interval=15):
    """Report on registered agents and active streaming tasks"""
    while True:
        print("\n[a2a_edge] --- AGENT REGISTRY STATUS ---")
        if not registered_agents:
            print("[a2a_edge] No agents currently registered.")
        else:
            for agent_name, info in registered_agents.items():
                endpoint = info.get("endpoint")
                timestamp = info.get("timestamp")
                print(f"  - {agent_name} | Endpoint: {endpoint} | Registered: {timestamp}")
        
        print("[a2a_edge] --- ACTIVE STREAMING TASKS ---")
        if not streaming_tasks:
            print("[a2a_edge] No active streaming tasks.")
        else:
            for stream_key in streaming_tasks:
                print(f"  - {stream_key}")
        
        print(f"[a2a_edge] Time: {datetime.datetime.now().isoformat()}")
        print("[a2a_edge] -------------------------------\n")
        await asyncio.sleep(interval)

# --- Main Function ---
async def main():
    """Main function to run the A2A edge handler"""
    print("[a2a_edge] Starting A2A edge handler...")
    
    # Connect to Redis
    redis_client = aioredis.from_url(build_redis_url())

    async def handler_with_redis(env):
        await handle_a2a_request(env, redis_client)
    
    try:
        # Create bus adapter
        adapter = BusAdapterV2(
            agent_id="a2a_edge",
            core_handler=handler_with_redis,
            redis_client=redis_client,
            patterns=[INBOX_CHANNEL],
            group="a2a_edge_group"
        )
        
        # Add registration handler
        await adapter.add_subscription(REGISTER_CHANNEL, handle_registration)
        
        # Add task cancellation handler using the A2A cancel channel
        cancel_channel = keys.a2a_inbox("cancel")  # "AG1:a2a:agent:cancel:inbox"
        await adapter.add_subscription(cancel_channel, handle_task_cancel)
        
        # Start the adapter
        await adapter.start()
        
        # Start the reporter task
        asyncio.create_task(reporter_task(interval=15))
        
        print(f"[a2a_edge] Subscribed to channels: {adapter.list_subscriptions()}")
        print("[a2a_edge] A2A edge handler is running...")
        
        # Keep the process alive
        await asyncio.Event().wait()
    
    except KeyboardInterrupt:
        print("[a2a_edge] Shutting down...")
    except Exception as e:
        print(f"[a2a_edge] Unexpected error: {str(e)}")
        print(traceback.format_exc())
    finally:
        # Close Redis connection
        await redis_client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
