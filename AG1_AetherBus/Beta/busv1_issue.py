import json
import urllib.request
from dotenv import load_dotenv
load_dotenv()

import uuid
import json
from AG1_AetherBus.envelope import Envelope
import os
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from AG1_AetherBus.keys import StreamKeyBuilder
from typing import Any, Dict, Optional, Union, Callable 

import asyncio
import inspect
import traceback 

# --- Configurable Redis connection ---
REDIS_HOST = os.getenv("REDIS_HOST", "forge.evasworld.net")
REDIS_PORT = int(os.getenv("REDIS_PORT", 8081))
REDIS_USERNAME = os.getenv("REDIS_USERNAME","admin")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "UltraSecretRoot123")

STREAM_MAXLEN = int(os.getenv("BUS_STREAM_MAXLEN", 10000))  # For dev/demo, tune as needed
ENVELOPE_SIZE_LIMIT = 128 * 1024  # 128 KB

key_builder = StreamKeyBuilder()

def extract_user_id_from_channel(ch):
    return ch.split(".")[1] if ch.startswith("user.") else "unknown"

# --- Utility: Build Redis URL with optional auth ---
def build_redis_url():
    user = REDIS_USERNAME
    pwd = REDIS_PASSWORD
    host = REDIS_HOST # Use REDIS_HOST from .env or default
    port = REDIS_PORT # Use REDIS_PORT from .env or default
    print(f'Build url {user}  {host} {port}')
    if user and pwd:
        return f"redis://{user}:{pwd}@{host}:{port}"
    elif pwd: # Support for password-only Redis (e.g. some cloud providers)
        return f"redis://:{pwd}@{host}:{port}"
    else:
        return f"redis://{host}:{port}"

# --- Utility: Ensure a Redis Stream Group Exists ---
async def ensure_group(redis, channel: str, group: str):
    try:
        # mkstream=True creates the stream if it doesn't exist
        await redis.xgroup_create(name=channel, groupname=group, id='0-0', mkstream=True)
        print(f"[BUS][Group] Created consumer group '{group}' for channel '{channel}'.")
    except ResponseError as e:
        if "BUSYGROUP" in str(e):
            # Group already exists — no issue
            # print(f"[BUS][Group] Consumer group '{group}' already exists for channel '{channel}'.")
            pass
        else:
            print(f"[BUS][Group][ERROR] Failed to create consumer group '{group}' for channel '{channel}': {e}")
            raise


# --- Envelope Publisher (with Discovery + Size Guard) ---
async def publish_envelope(redis, channel: str, env: Envelope):
    # Ensure env is an Envelope object before converting to dict
    if not isinstance(env, Envelope):
        raise TypeError("Expected 'env' to be an Envelope object.")
        
    data = json.dumps(env.to_dict())

    # Enforce payload size limit
    if len(data.encode("utf-8")) > ENVELOPE_SIZE_LIMIT:
        raise ValueError(
            f"Envelope exceeds {ENVELOPE_SIZE_LIMIT} bytes. Offload large payloads to object storage. Channel: {channel}, Envelope ID: {env.envelope_id}"
        )

    # Publish the envelope to Redis stream
    # Redis Streams are created implicitly on first XADD, so no need for redis.exists(channel) check
    await redis.xadd(channel, {"data": data}, maxlen=STREAM_MAXLEN)
    print(f"[BUS][Publish] Published Envelope {env.envelope_id} to channel: {channel}. Size: {len(data.encode('utf-8'))} bytes.")

    # The "discovery" mechanism via "user.discovery" stream is an older pattern.
    # In the current BusAdapterV2 and edge handler design, subscriptions are explicit.
    # This block can likely be removed or refactored if not actively used for discovery.
    # if not stream_exists: # This check is unreliable as xadd creates stream
    #     discovery_env = Envelope(
    #         role="system", # Changed role to system for discovery events
    #         content={"stream": channel, "action": "stream_created"},
    #         user_id=env.user_id or "unknown",
    #         agent_name="bus_discovery",
    #         envelope_type="discovery"
    #     )
    #     await redis.xadd("user.discovery", {"data": json.dumps(discovery_env.to_dict())})


# --- Core Subscriber (Consumer Group Model) ---
async def subscribe(
    redis: Redis, # Type hint for Redis client
    channel: str,
    callback: Callable[[Envelope], None], # Callback now expects Envelope directly
    group: str = "corebus",
    consumer: str = None,
    block_ms: int = 1000,
    dead_letter_max_retries: int = 3
):
    """
    Subscribes to a Redis Stream using a consumer group.
    Messages are passed to the callback as deserialized Envelope objects.
    Handles retries and acknowledges messages.
    """
    await ensure_group(redis, channel, group)
    consumer = consumer or f"{group}-default"
    print(f"[BUS][subscribe] Subscribing to channel: {channel}, group: {group}, consumer: {consumer}")
    retry_counts = {}

    while True:
        try:
            # Read messages from the stream for this consumer group
            results = await redis.xreadgroup(
                group, consumer, streams={channel: '>'}, count=1, block=block_ms
            )
            
            if not results:
                continue # No new messages within the block time, continue polling

            for stream_name, messages in results:
                for msg_id, fields in messages:
                    raw_data_bytes = fields.get(b"data") # Expecting bytes from xadd
                    
                    if raw_data_bytes is None:
                        print(f"[BUS][WARN] Message {msg_id} on {channel} has no 'data' field. Acknowledging and skipping.")
                        await redis.xack(channel, group, msg_id) # Acknowledge malformed message
                        continue

                    payload_str = None
                    try:
                        payload_str = raw_data_bytes.decode('utf-8')
                        # Check for hidden null bytes if that was an issue
                        if '\x00' in payload_str:
                            print(f"[BUS][WARN] Message {msg_id} contains NULL bytes. Cleaning.")
                            payload_str = payload_str.replace('\x00', '') # Remove null bytes
                    except UnicodeDecodeError as ude:
                        print(f"[BUS][ERROR][Subscribe] UnicodeDecodeError for message {msg_id} on {channel}: {ude}. Raw bytes (first 100): {raw_data_bytes[:100]}")
                        await redis.xack(channel, group, msg_id) # Acknowledge malformed message
                        continue # Skip to next message

                    # Use repr() for logging to show exact string content including newlines/quotes
                    print(f"[BUS][DEBUG] Parsing message {msg_id}. Len: {len(payload_str)}. Data (repr): '{repr(payload_str[:120])}'...")

                    try:
                        payload_dict = json.loads(payload_str)
                        env = Envelope.from_dict(payload_dict)
                        
                        print(f"[BUS][subscribe] Calling callback for message {msg_id} on {channel}")
                        await callback(env) # Pass the Envelope object directly
                        await redis.xack(channel, group, msg_id) # Acknowledge successful processing
                        if msg_id in retry_counts:
                            del retry_counts[msg_id]
                    except json.JSONDecodeError as e:
                        print(f"[BUS][ERROR][Subscribe] Malformed envelope (JSONDecodeError) for message {msg_id} on {channel}: {e}")
                        print(f"--- PROBLEMATIC JSON STRING (Len: {len(payload_str)}) ---")
                        print(payload_str) # PRINT THE ENTIRE STRING
                        print(f"--- END PROBLEMATIC JSON STRING ---")
                        await redis.xack(channel, group, msg_id) # Acknowledge malformed message
                    except Exception as e:
                        print(f"[BUS][ERROR][Subscribe] Error processing message {msg_id} on {channel} in callback: {e}")
                        traceback.print_exc()

                        retry_counts[msg_id] = retry_counts.get(msg_id, 0) + 1
                        if retry_counts[msg_id] > dead_letter_max_retries:
                            print(f"[BUS][DEAD] Giving up on message {msg_id} after {dead_letter_max_retries} retries. Acknowledging.")
                            await redis.xack(channel, group, msg_id)
                            retry_counts.pop(msg_id)
                        else:
                            print(f"[BUS][RETRY] Retrying message {msg_id}. Attempt {retry_counts[msg_id]}/{dead_letter_max_retries}.")
                            # Message remains in pending entries for retry

        except asyncio.CancelledError:
            print(f"[BUS][subscribe] Subscription for channel '{channel}' cancelled.")
            raise # Re-raise to allow proper task cleanup
        except RedisConnectionError as err:
            print(f"[BUS][ERROR] Redis connection error during subscribe on {channel}: {err}. Retrying after delay.")
            await asyncio.sleep(1) # Short delay before retrying connection
        except Exception as err:
            print(f"[BUS][ERROR] Unexpected error in subscribe loop for {channel}: {err}")
            traceback.print_exc()

# Simple non-group subscriber (kept for now, but consider if it's still needed)
async def subscribe_simple(redis: Redis, stream: str, callback: Callable[[Envelope], None], poll_delay: int = 1):
    """
    Simple subscriber that reads from a stream without using consumer groups.
    Messages are passed to the callback as deserialized Envelope objects.
    """
    print(f"[BUS][subscribe_simple] Subscribing to stream: {stream}")
    last_id = "$"  # Only get new messages
    while True:
        try:
            results = await redis.xread({stream: last_id}, block=poll_delay * 1000, count=10)
            if not results:
                continue
            
            for s, messages in results:
                for msg_id, fields in messages:
                    raw_data_bytes = fields.get(b"data")
                    if raw_data_bytes:
                        try:
                            payload_str = raw_data_bytes.decode('utf-8')
                            env = Envelope.from_dict(json.loads(payload_str))
                            await callback(env)
                            last_id = msg_id # Advance cursor only on successful processing
                        except json.JSONDecodeError as e:
                            print(f"[BUS][ERROR][SubscribeSimple] Malformed envelope on {stream}: {e}. Raw: {raw_data_bytes[:100]}")
                        except Exception as e:
                            print(f"[BUS][ERROR][SubscribeSimple] Error processing message on {stream}: {e}")
                    else:
                        print(f"[BUS][WARN][SubscribeSimple] Message {msg_id} on {stream} has no 'data' field.")
        except asyncio.CancelledError:
            print(f"[BUS][subscribe_simple] Subscription for stream '{stream}' cancelled.")
            raise
        except RedisConnectionError as e:
            print(f"[BUS][ERROR][SubscribeSimple] Redis connection error for {stream}: {e}. Retrying after delay.")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[BUS][ERROR][SubscribeSimple] Unexpected error in loop for {stream}: {e}")
            traceback.print_exc()

async def main():
    print(f'Host : {REDIS_HOST}')
    # Example usage:
    # redis_client = await Redis.from_url(build_redis_url())
    # await publish_envelope(redis_client, "my_channel", Envelope(role="test", content={"text": "hello"}))
    # await subscribe(redis_client, "my_channel", lambda env: print(f"Received: {env.content['text']}"))
    # await redis_client.aclose()

if __name__ == "__main__":
    asyncio.run(main())