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
#from bus import publish_envelope

import asyncio
import inspect

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
                    print(f"Received messages in stream results: {str(raw)[:40]}")
                    if not raw:
                        continue

                    try:
                        env = Envelope.from_dict(json.loads(raw))
                        # Optional: Add tracing hop
                        env.add_hop("bus_subscribe")
                        print(f"In subscribes: {msg_id}")
                        await callback(env)
                        await redis.xack(channel, group, msg_id)
                        if msg_id in retry_counts:
                            del retry_counts[msg_id]

                    except Exception as e:
                        print(f"[BUS][ERROR][Subsribe] Malformed envelope on {channel}: {e}")

                        retry_counts[msg_id] = retry_counts.get(msg_id, 0) + 1
                        if retry_counts[msg_id] > dead_letter_max_retries:
                            print(f"[BUS][DEAD] Giving up on {msg_id} after {dead_letter_max_retries} retries.")
                            await redis.xack(channel, group, msg_id)
                            retry_counts.pop(msg_id)

        except Exception as err:
            print(f"[BUS][ERROR] Subscribe error on {channel}: {err}")


# Simple non-group subscriber
async def subscribe_simple(redis, stream: str, callback, poll_delay=1):
    print(f'subscibe {redis} stream {stream}')
    last_id = "$"  # Only get new messages
    while True:
        try:
            #print('x')
            results = await redis.xreadgroup({stream: last_id}, block=poll_delay * 1000, count=10)
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
