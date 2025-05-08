from dotenv import load_dotenv
load_dotenv()
import asyncio
import redis.asyncio as aioredis
from AG1_AetherBus.bus import publish_envelope, subscribe, ensure_group, REDIS_HOST, REDIS_PORT
from AG1_AetherBus.envelope import Envelope
import json
import sys
import time

async def print_envelope(env):
    print(f"[{getattr(env, 'ts', None)}] {getattr(env, 'channel', '?')} | {getattr(env, 'id', '?')}")
    print(f"Headers: {getattr(env, 'headers', {})}")
    print(f"Payload: {getattr(env, 'payload', env.content)}")
    print(f"Trace: {getattr(env, 'trace', [])}")
    print("---\n")

async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m AG1_AetherBus.tail <channel>")
        sys.exit(1)
    channel = sys.argv[1]
    print(f"Tailing channel: {channel}\n")

    redis = await aioredis.from_url(build_redis_url())

    # Print backlog first (all history)
    try:
        messages = await redis.xrange(channel, '-', '+')
        for msg_id, msg_data in messages:
            data = json.loads(msg_data[b'data'].decode())
            env = Envelope.from_dict(data)
            await print_envelope(env)
    except Exception as e:
        print(f"[tail] Could not read backlog: {e}")

    # Now switch to live tailing (like before)
    await subscribe(redis, channel, print_envelope, group="tail", consumer=f"tail-{int(time.time())}", block_ms=1000)
    await redis.aclose()

def build_redis_url():
    user = os.getenv("REDIS_USERNAME")
    pwd = os.getenv("REDIS_PASSWORD")
    host = REDIS_HOST
    port = REDIS_PORT
    if user and pwd:
        return f"redis://{user}:{pwd}@{host}:{port}"
    elif pwd:
        return f"redis://:{pwd}@{host}:{port}"
    else:
        return f"redis://{host}:{port}"

if __name__ == "__main__":
    asyncio.run(main())
