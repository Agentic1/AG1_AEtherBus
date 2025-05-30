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
    max_responses: int = 10 # This parameter is not currently used in the loop, but kept for signature
) -> AsyncIterator[Envelope]:
    """
    Publish request_env → target_stream, then stream back every reply
    on request_env.reply_to until timeout.
    Yields deserialized Envelope objects.
    """
    # Ensure reply_to is set for the request
    if not request_env.reply_to:
        request_env.reply_to = f"AG1:rpc_streams:{request_env.agent_name or 'unknown_rpc_caller'}:{request_env.correlation_id or str(uuid.uuid4())[:8]}"
        print(f"[RPC][Stream][WARN] request_env.reply_to was not set. Using generated: {request_env.reply_to}")

    # 1) Publish the request
    await publish_envelope(redis, target_stream, request_env)
    print(f"[RPC][Stream] Request published to {target_stream} for CID: {request_env.correlation_id}")

    # 2) Repeatedly read one at a time until timeout
    last_id = "$"
    deadline = time.time() + timeout
    responses_count = 0

    while True:
        block_ms = int(max(0, deadline - time.time()) * 1000)
        if block_ms <= 0:
            print(f"[RPC][Stream] Timeout reached for CID: {request_env.correlation_id}")
            break # Timeout

        try:
            results = await redis.xread(
                {request_env.reply_to: last_id},
                count=1, # Read one entry at a time
                block=block_ms
            )
        except asyncio.TimeoutError:
            print(f"[RPC][Stream] asyncio.TimeoutError during xread for CID: {request_env.correlation_id}")
            break
        except Exception as e:
            print(f"[RPC][Stream][ERROR] Unexpected error during xread for CID: {request_env.correlation_id}: {e}")
            traceback.print_exc()
            break

        if not results:
            print(f"[RPC][Stream] No new results from xread for CID: {request_env.correlation_id}. Continuing or timing out.")
            continue # No new messages within the block time, continue checking deadline

        stream_key, entries = results[0]
        entry_id, fields = entries[0]
        last_id = entry_id  # advance the cursor

        raw_data_bytes = fields.get(b"data") # Prefer b"data" as that's what publish_envelope uses
        if raw_data_bytes is None:
            print(f"[RPC][Stream][WARN] Received stream entry {entry_id} with no 'data' field for CID: {request_env.correlation_id}. Skipping.")
            continue # Skip malformed entry

        try:
            payload_str = raw_data_bytes.decode('utf-8') if isinstance(raw_data_bytes, (bytes, bytearray)) else raw_data_bytes
            response_env = Envelope.from_dict(json.loads(payload_str))
            responses_count += 1
            print(f"[RPC][Stream] Yielding response {responses_count} for CID: {request_env.correlation_id}")
            yield response_env
            
            # Optional: Break if max_responses reached, though not currently enforced in loop
            # if max_responses > 0 and responses_count >= max_responses:
            #     print(f"[RPC][Stream] Max responses reached for CID: {request_env.correlation_id}")
            #     break

        except json.JSONDecodeError as e:
            print(f"[RPC][Stream][ERROR] Failed to decode JSON payload for CID: {request_env.correlation_id}: {e}. Raw: {payload_str[:100]}...")
            continue
        except Exception as e:
            print(f"[RPC][Stream][ERROR] Unexpected error processing stream entry for CID: {request_env.correlation_id}: {e}")
            traceback.print_exc()
            continue

    print(f"[RPC][Stream] Finished streaming for CID: {request_env.correlation_id}. Total responses: {responses_count}")


async def bus_rpc_call( # This function's contract remains: returns Optional[str]
    redis: Redis,
    target_stream: str,
    request_env: Envelope,
    timeout: float = 5.0
) -> Optional[str]: # <--- IMPORTANT: Still returns Optional[str]
    """
    Publish request_env → target_stream, wait for one reply on request_env.reply_to.
    Returns the raw JSON string payload of the response, or None on timeout/malformed message.
    This is the low-level RPC call that returns the raw string from Redis.
    """
    print(f'----> [RPC] bus_rpc_call (raw string): Publishing request to {target_stream} for CID: {request_env.correlation_id}')

    # Ensure reply_to is set for this RPC call
    if not request_env.reply_to:
        request_env.reply_to = f"AG1:rpc_raw_replies:{request_env.agent_name or 'unknown_rpc_caller'}:{request_env.correlation_id or str(uuid.uuid4())[:8]}"
        print(f"[RPC][WARN] request_env.reply_to was not set. Using generated: {request_env.reply_to}")

    # 1) Publish the request envelope
    await publish_envelope(redis, target_stream, request_env)
    print(f"[RPC] Request published to {target_stream} for CID: {request_env.correlation_id}")

    # 2) Wait for one reply on the designated reply_to stream
    deadline = time.time() + timeout
    last_id = "$" # Start reading from the latest entry

    while True:
        block_ms = int(max(0, deadline - time.time()) * 1000)
        if block_ms <= 0:
            print(f"[RPC] Timeout reached for CID: {request_env.correlation_id}")
            return None # Timeout

        try:
            results = await redis.xread(
                {request_env.reply_to: last_id},
                count=1,
                block=block_ms
            )
            print(f'[RPC] xread result for CID {request_env.correlation_id}: {results}')

            if not results:
                continue # No new messages within the block time, continue checking deadline

            # Unpack the single entry
            stream_key, entries = results[0]
            entry_id, fields = entries[0]
            last_id = entry_id # Advance the cursor

            raw_data_bytes = fields.get(b"data") # Prefer b"data" as that's what publish_envelope uses
            if raw_data_bytes is None:
                print(f"[RPC][WARN] Received stream entry {entry_id} with no 'data' field for CID: {request_env.correlation_id}. Skipping.")
                continue # Skip malformed entry

            try:
                payload_str = raw_data_bytes.decode('utf-8') if isinstance(raw_data_bytes, (bytes, bytearray)) else raw_data_bytes
                
                # Attempt to load JSON to ensure it's a valid JSON string, but return the string
                # This ensures we only return valid JSON strings, not arbitrary text.
                json.loads(payload_str) 
                print(f"[RPC] Received valid JSON string response for CID: {request_env.correlation_id}")
                return payload_str # Return the raw JSON string
            except json.JSONDecodeError as e:
                print(f"[RPC][ERROR] Failed to decode JSON payload for CID: {request_env.correlation_id}: {e}. Raw: {payload_str[:100]}...")
                return None # Indicate failure to get a valid JSON string
            except Exception as e:
                print(f"[RPC][ERROR] Unexpected error processing stream entry for CID: {request_env.correlation_id}: {e}")
                traceback.print_exc()
                return None # Indicate failure

        except asyncio.TimeoutError:
            print(f"[RPC] asyncio.TimeoutError during xread for CID: {request_env.correlation_id}")
            return None
        except Exception as e:
            print(f"[RPC][ERROR] Unexpected error during xread for CID: {request_env.correlation_id}: {e}")
            traceback.print_exc()
            raise # Re-raise critical Redis connection errors, etc.

    print(f"[RPC] No valid response received within timeout for CID: {request_env.correlation_id}")
    return None # No valid response received within the loop/timeout


async def bus_rpc_envelope( # This function's contract remains: returns Optional[Envelope]
    redis_client: Redis,
    target_inbox: str,
    request_envelope: Envelope,
    timeout: float = 10.0
) -> Optional[Envelope]:
    """
    Performs an RPC-style call over the bus, returning a deserialized Envelope object.
    This function wraps `bus_rpc_call` to handle deserialization.
    Returns Envelope, or None if no response or if the response is malformed.
    """
    print(f"[RPC][bus_rpc_envelope] Calling bus_rpc_call (raw string) for CID: {request_envelope.correlation_id} to target: {target_inbox} | reply_to: {request_envelope.reply_to}")
    
    # Ensure reply_to is set for bus_rpc_call to listen on
    if not request_envelope.reply_to:
        # This fallback should ideally be handled by the caller (e.g., A2AProxy using kb.a2a_response)
        # but as a fallback for the raw call:
        request_envelope.reply_to = f"AG1:rpc_replies:{request_envelope.agent_name or 'unknown'}:{request_envelope.correlation_id or str(uuid.uuid4())}"
        print(f"[RPC][bus_rpc_envelope][WARN] request_envelope.reply_to was not set. Using fallback: {request_envelope.reply_to}")

    # Call the low-level RPC function that returns the raw JSON string
    raw_response_json_str = await bus_rpc_call(redis_client, target_inbox, request_envelope, timeout)

    if raw_response_json_str is None: # Timeout or no valid JSON string from bus_rpc_call
        print(f"[RPC][bus_rpc_envelope][ERROR] No raw JSON string response or timeout from bus_rpc_call for CID {request_envelope.correlation_id}.")
        return None

    try:
        response_dict = json.loads(raw_response_json_str)
        response_envelope = Envelope.from_dict(response_dict)
        print(f"[RPC][bus_rpc_envelope] Successfully deserialized Envelope for CID {request_envelope.correlation_id}. Content: {str(response_envelope.content)[:100]}...")
        return response_envelope
    except json.JSONDecodeError as e:
        print(f"[RPC][bus_rpc_envelope][ERROR] Failed to deserialize raw_response string to Envelope: {e}. Raw: {raw_response_json_str[:200]}...")
        return None # Indicate failure to get a valid Envelope
    except Exception as e_general: # Catch other potential errors during Envelope.from_dict
        print(f"[RPC][bus_rpc_envelope][ERROR] Unexpected error during Envelope reconstruction: {e_general}. Raw: {raw_response_json_str[:200]}...")
        traceback.print_exc()
        return None # Indicate failure