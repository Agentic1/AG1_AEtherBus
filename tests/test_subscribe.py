"""
test_subscribe.py

Purpose:
    Subscribes to an agent's inbox stream on the AG1 core bus and prints received Envelope messages.

Usage:
    $ python test_subscribe.py

Expected Output:
    - Prints subscription details and any Envelope received on the subscribed stream.
    - On success, output will show details of each Envelope as it arrives (role, content, session).

Verifying:
    Use another script (e.g., test_send.py) to publish a message to the stream and confirm it is received here.

Note:
    - Requires a running Redis instance and correct core bus configuration.
    - The script subscribes to agent_name='pa0'; edit as needed for other agents/streams.
"""

# test_subscribe.py
import asyncio
from redis.asyncio import Redis
from AG1_AetherBus.bus import subscribe, build_redis_url, ensure_group
from AG1_AetherBus.envelope import Envelope


GROUP = "pa0"
STREAM = "AG1:agent:pa0:inbox"  # Could be agent inbox too

async def handle(env: Envelope):
    print("🟢 Envelope received:")
    print(f"Role: {env.role}")
    print(f"Content: {env.content}")
    print(f"Session: {env.session_code}")
    print("===")

async def main():
    redis = Redis.from_url(build_redis_url())

    await ensure_group(redis, STREAM, GROUP)
    print(f"📡 Subscribing to stream: {STREAM} as group: {GROUP}")
    await subscribe(redis, STREAM, handle, GROUP)

    await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
