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



# --- Utility: Ensure a Redis Stream Group Exists ---
async def ensure_group(redis, channel: str, group: str):
    try:
        await redis.xgroup_create(name=channel, groupname=group, id='0-0', mkstream=True)
        print(f"[BUS][Group] Created consumer group '{group}' for channel '{channel}'.")
    except ResponseError as e:
        if "BUSYGROUP" in str(e):
            # Group already exists — no issue
            pass
        else:
            raise


# --- Envelope Publisher (with Discovery + Size Guard) ---
async def publish_envelope(redis, channel: str, env: Envelope):
    data = json.dumps(env.to_dict())

    # Enforce payload size limit
    if len(data.encode("utf-8")) > ENVELOPE_SIZE_LIMIT:
        raise ValueError(
            f"Envelope exceeds {ENVELOPE_SIZE_LIMIT} bytes. Offload large payloads to object storage."
        )

    # Check if stream exists *before* publishing
    stream_exists = await redis.exists(channel)

    # Publish the envelope to Redis stream
    await redis.xadd(channel, {"data": data}, maxlen=STREAM_MAXLEN)

    # Trigger discovery if this is a new stream
    if not stream_exists:
        print('[BUS][Publish] no stream')
        discovery_env = Envelope(
            role="user",
            content={"stream": channel},
            user_id=env.user_id or "unknown",
            agent_name="bus_discovery",
            envelope_type="discovery"
        )
        await redis.xadd("user.discovery", {"data": json.dumps(discovery_env.to_dict())})

# --- Core Subscriber (Consumer Group Model) ---

async def subscribe(
    redis,
    channel: str,
    callback,
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
    print(f"[BUS][subscribe]")
    retry_counts = {}

    while True:
        try:
            #print('x')
            results = await redis.xreadgroup(
                group, consumer, streams={channel: '>'}, count=1, block=block_ms
            )
            #print(f"subscribe: results={results}")
            if not results:
                continue
            
            for stream, messages in results:
                #print(f"Received messages from stream: {stream} messages {messages}")
                for msg_id, fields in messages:
                    #print(f"Received messages in stream results id: {msg_id}")
                    
                    #raw = fields.get("data") or fields.get(b"data")
                    raw = fields.get("envelope") or fields.get("data") or fields.get(b"envelope") or fields.get(b"data")
                    print(f"Received messages in stream results: {str(raw)[:90]}")
                    if not raw:
                        continue


                    # --- START DIAGNOSTIC CHANGES ---
                    json_string_to_parse = None
                    if isinstance(raw, bytes):
                        try:
                            # Try decoding as UTF-8 first
                            json_string_to_parse = raw.decode('utf-8').strip()
                            # If successful, check for unexpected null bytes that print() might hide
                            if '\x00' in json_string_to_parse:
                                print(f"[BUS][WARN] Decoded string contains NULL bytes. Original len: {len(raw)}, Decoded len: {len(json_string_to_parse)}")
                                # Optionally, replace or escape null bytes if that's desired,
                                # though this usually indicates a deeper data corruption issue.
                                # json_string_to_parse = json_string_to_parse.replace('\x00', '[NULL_BYTE]')
                        except UnicodeDecodeError as ude:
                            print(f"[BUS][ERROR][Subscribe] UnicodeDecodeError for raw bytes: {ude}")
                            print(f"  Problematic raw bytes (first 100): {raw[:100]}")
                            # Attempt to decode with 'latin-1' or 'replace' to see the content if UTF-8 fails
                            try:
                                fallback_str = raw.decode('latin-1', errors='replace')
                                print(f"  Fallback decoded (latin-1, replace errors) (first 100): {fallback_str[:100]}")
                            except:
                                pass # Fallback decoding also failed
                            # For now, let it fall through so json.loads() fails and we see the problematic string
                            # If it's not valid UTF-8, json.loads(raw) (if raw is bytes) would also fail.
                            # We need to ensure json_string_to_parse is set for the next block if we want to attempt parsing.
                            # If we can't decode, we probably shouldn't try to json.loads it.
                            # Let's make json_string_to_parse the raw bytes representation for logging if decode fails.
                            json_string_to_parse = str(raw) # So it's not None and gets logged by the except block

                    elif isinstance(raw, str): 
                        json_string_to_parse = raw.strip()
                        if '\x00' in json_string_to_parse:
                            print(f"[BUS][WARN] Raw string contains NULL bytes. Len: {len(json_string_to_parse)}")
                            # json_string_to_parse = json_string_to_parse.replace('\x00', '[NULL_BYTE]')
                    else:
                        print(f"[BUS][ERROR][Subscribe] 'raw' data is of unexpected type: {type(raw)}")
                        continue 

                    # This debug print should now be more revealing if there are hidden chars
                    print(f"[BUS][DEBUG] String to parse. Len: {len(json_string_to_parse)}. Data (repr): '{repr(json_string_to_parse[:120])}'...") # Use repr()

                    if json_string_to_parse is None: 
                        print(f"[BUS][WARN] json_string_to_parse is None after processing 'raw'. Skipping.")
                        continue
                    # --- END DIAGNOSTIC CHANGES ---



                    try:
                        payload_dict = json.loads(json_string_to_parse)
                        env = Envelope.from_dict(payload_dict) #json.loads(raw)
                        # Optional: Add tracing hop
                        env.add_hop("bus_subscribe")
                        print(f"In subscribes: {msg_id}")
                        await callback(env)
                        await redis.xack(channel, group, msg_id)
                        if msg_id in retry_counts:
                            del retry_counts[msg_id]
                    except json.JSONDecodeError as e: # Catch specifically JSONDecodeError
                        print(f"[BUS][ERROR][Subscribe] Malformed envelope (JSONDecodeError) on {channel}: {e}")
                        print(f"--- PROBLEMATIC JSON STRING (Len: {len(json_string_to_parse)}) ---")
                        print(json_string_to_parse) # PRINT THE ENTIRE STRING
                        print(f"--- END PROBLEMATIC JSON STRING ---")
                    except Exception as e:
                        print(f"[BUS][ERROR][Subsribe] Malformed envelope on {channel}: {e}")
                        traceback.print_exc()


                        retry_counts[msg_id] = retry_counts.get(msg_id, 0) + 1
                        if retry_counts[msg_id] > dead_letter_max_retries:
                            print(f"[BUS][DEAD] Giving up on {msg_id} after {dead_letter_max_retries} retries.")
                            await redis.xack(channel, group, msg_id)
                            retry_counts.pop(msg_id)

        except Exception as err:
            print(f"[BUS][ERROR] Subscribe error on {channel}: {err}")
            traceback.print_exc()


# Simple non-group subscriber
async def subscribe_simple(redis, stream: str, callback, poll_delay=1):
    print(f'subscibe {redis} stream {stream}')
    last_id = "$"  # Only get new messages
    while True:
        try:
            #print('x')
            results = await redis.xreadgroup({stream: last_id}, block=poll_delay * 1000, count=10)
            #results = await redis.xread({stream: last_id}, block=poll_delay * 1000, count=10)
            #print(f"subscribe_simple: results={results} s{stream}")
            for s, messages in results:
                for msg_id, fields in messages:
                    data = fields.get("data")
                    if data:
                        try:
                            env = Envelope.from_dict(json.loads(data))
                            await callback(env)
                            last_id = msg_id
                        except Exception as e:
                            print(f"[BUS][ERROR] Malformed envelope: {e}")
        except Exception as e:
            print(f"[BUS][ERROR] Failed to read from {stream}: {e}")

# --- Helper: Build Redis URL with optional auth ---
def build_redis_url():
    user = REDIS_USERNAME
    pwd = REDIS_PASSWORD
    print(f'Build url {user}  {REDIS_HOST} {REDIS_PORT}')
    if user and pwd:
        return f"redis://{user}:{pwd}@{REDIS_HOST}:{REDIS_PORT}"
    elif pwd:
        return f"redis://:{pwd}@{REDIS_HOST}:{REDIS_PORT}"
    else:
        return f"redis://{REDIS_HOST}:{REDIS_PORT}"

async def main():
    print(f'Host : {REDIS_HOST}')
    #redis = await aioredis.from_url(build_redis_url())
    #redis_conn = Redis.from_url(build_redis_url())
    # Example usage:
    # await publish_envelope(redis, "my_channel", Envelope())
    # await subscribe(redis, "my_channel", my_callback)
    #await redis_conn.aclose()

if __name__ == "__main__":
    asyncio.run(main())
