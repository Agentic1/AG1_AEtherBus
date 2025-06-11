# AG1_AetherBus/rpc.py

import json, time
import asyncio

import uuid

from typing import Any, Dict, Optional, Union
from redis.asyncio import Redis
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.agent_bus_minimal import publish_envelope
from typing import AsyncIterator

async def bus_rpc_stream(
    redis: Redis,
    target_stream: str,
    request_env: Envelope,
    timeout: float = 5.0,
    max_responses: int = 10
) -> AsyncIterator[Envelope]:
    """
    Publish request_env → target_stream, then stream back every reply
    on request_env.reply_to until timeout.
    """
    # ensure we have a reply_to
    if not request_env.reply_to:
        request_env.reply_to = f"AG1:agent:{request_env.user_id}:outbox"

    # 1) push the request  
    await redis.xadd(target_stream, {"data": request_env.json()})

    # 2) repeatedly read one at a time until timeout  
    block_ms = int(timeout * 1000)
    last_id  = "$"
    while True:
        try:
            results = await redis.xread(
                {request_env.reply_to: last_id},
                count=1,
                block=block_ms
            )
        except TimeoutError:
            break
        if not results:
            break

        stream_key, entries = results[0]
        entry_id, fields   = entries[0]
        last_id = entry_id  # advance the cursor

        raw = fields.get(b"data")
        payload = raw.decode() if isinstance(raw, (bytes,bytearray)) else raw
        yield Envelope.from_dict(json.loads(payload))



async def bus_rpc_call(
    redis: Redis,
    target_stream: str,
    request_env: Envelope,
    timeout: float = 15.0
) -> Optional[str]:
    print(f'----> [RPC] bus_rpc_call initiated. Target: {target_stream}, CID: {request_env.correlation_id}, ReplyTo: {request_env.reply_to}')
    print(f"\n---<>-----\n[RPC][DEBUG_PUBLISH] About to publish to target_stream='{target_stream}' (type: {type(target_stream)}), request_env.reply_to='{request_env.reply_to}' (type: {type(request_env.reply_to)})")
    await publish_envelope(redis, target_stream, request_env)
    
    deadline = time.time() + timeout
    last_id = "$" 

    while time.time() < deadline:
        current_block_ms = int(max(1, (deadline - time.time()) * 1000)) # Ensure block_ms is at least 1

        print(f'[RPC] Attempting XREAD on {request_env.reply_to} (last_id: {last_id}), block_ms: {current_block_ms}, CID: {request_env.correlation_id}')
        results = await redis.xread(
            {request_env.reply_to: last_id},
            count=1,
            block=current_block_ms
        )
        
        if not results:
            print(f'[RPC] XREAD returned no results for {request_env.reply_to}, CID: {request_env.correlation_id}. Continuing or timing out.')
            # If xread returns empty, it means it blocked for current_block_ms and nothing arrived.
            # The outer while loop will check the deadline.
            continue

        print(f'[RPC] Raw XREAD results for {request_env.reply_to}, CID: {request_env.correlation_id}: {results}')
        stream_key, entries = results[0]
        entry_id, fields = entries[0]
        last_id = entry_id 

        raw_payload_bytes = fields.get(b"data") or fields.get("data")
        if raw_payload_bytes is None:
            print(f'[RPC][WARN] Message {entry_id} on {request_env.reply_to} has no "data" field. Skipping. CID: {request_env.correlation_id}')
            continue
        
        payload_str = raw_payload_bytes.decode() if isinstance(raw_payload_bytes, bytes) else raw_payload_bytes

        try:
            json.loads(payload_str) 
            print(f'[RPC] SUCCESS: Received valid JSON reply on {request_env.reply_to} for CID: {request_env.correlation_id}. Returning payload.')
            return payload_str # Return immediately upon finding a valid JSON message
        except json.JSONDecodeError:
            print(f'[RPC][WARN] Message {entry_id} on {request_env.reply_to} is not valid JSON: {payload_str[:100]}... Skipping. CID: {request_env.correlation_id}')
            continue 

    print(f'[RPC] TIMEOUT: No valid reply received on {request_env.reply_to} for CID {request_env.correlation_id} within {timeout}s.')
    return None

async def bus_rpc_envelope(
    redis_client: Redis,
    target_inbox: str,
    request_envelope: Envelope,
    timeout: float = 10.0
) -> Union[Envelope, Dict[str, Any], None]:
    """
    Performs an RPC-style call over the bus, returning a deserialized Envelope object.
    This wraps bus_rpc_call and deserializes its string output.
    Returns Envelope, a dict with "error", or None if no response.
    """
    print(f"[RPC][bus_rpc_envelope] Calling bus_rpc_call for CID: {request_envelope.correlation_id} to target: {target_inbox} | reply_to: {request_envelope.reply_to}")
    
    # Ensure reply_to is set for bus_rpc_call to listen on
    if not request_envelope.reply_to:
        # This should ideally be set by the caller (e.g., A2AProxy using kb.a2a_response)
        # but as a fallback:
        request_envelope.reply_to = f"AG1:rpc_reply:{request_envelope.agent_name or 'unknown'}:{request_envelope.correlation_id or str(uuid.uuid4())}"
        print(f"[RPC][bus_rpc_envelope][WARN] request_envelope.reply_to was not set. Using fallback: {request_envelope.reply_to}")

    raw_response_json_str = await bus_rpc_call(redis_client, target_inbox, request_envelope, timeout)

    if raw_response_json_str is None: # Timeout or no valid message from bus_rpc_call
        print(f"[RPC][bus_rpc_envelope][ERROR] No response or timeout from bus_rpc_call for CID {request_envelope.correlation_id}.")
        return {"error": "RPC Timeout or No Response"}

    if isinstance(raw_response_json_str, str):
        try:
            response_dict = json.loads(raw_response_json_str)
            # Reconstruct the Envelope object
            response_envelope = Envelope.from_dict(response_dict)
            print(f"[RPC][bus_rpc_envelope] Successfully deserialized Envelope for CID {request_envelope.correlation_id}. Content: {str(response_envelope.content)[:100]}...")
            return response_envelope
        except json.JSONDecodeError as e:
            print(f"[RPC][bus_rpc_envelope][ERROR] Failed to deserialize raw_response string to Envelope: {e}. Raw: {raw_response_json_str[:200]}...")
            return {"error": f"RPC Response Deserialization Error: {e} (Raw: {raw_response_json_str[:100]})"}
        except Exception as e_general: # Catch other potential errors during Envelope.from_dict
            print(f"[RPC][bus_rpc_envelope][ERROR] Unexpected error during Envelope reconstruction: {e_general}. Raw: {raw_response_json_str[:200]}...")
            traceback.print_exc()
            return {"error": f"RPC Envelope Reconstruction Error: {e_general}"}
    else:
        # This case should not be reached if bus_rpc_call returns Optional[str]
        print(f"[RPC][bus_rpc_envelope][WARN] Unexpected return type from bus_rpc_call: {type(raw_response_json_str)}. Expected str or None.")
        return {"error": f"RPC Unexpected Internal Return Type: {type(raw_response_json_str)}"}