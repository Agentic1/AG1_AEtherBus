"""
test_inject_reply.py

Purpose:
    Publishes a test Envelope message to a user's inbox stream on the AG1 core bus.

Usage:
    $ python test_inject_reply.py

Expected Output:
    - Prints the stream being published to and a confirmation message.
    - On success, a Redis key like 'AG1:user:Sean:inbox' will be created/updated.

Verifying:
    Use `redis-cli` or your Redis UI to check for the presence of the key and message.

Note:
    - Requires a running Redis instance and correct core bus configuration.
    - The message is sent to user_id='Sean'; edit as needed for other users.
"""

import asyncio
from redis.asyncio import Redis
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus import publish_envelope, build_redis_url
from AG1_AetherBus.keys import StreamKeyBuilder

async def inject_message():
    redis = Redis.from_url(build_redis_url())  # Use central config
    env = Envelope(
        role="agent",
        content={"text": "🧪 Hello Sean!"},
        user_id="Sean",
        envelope_type="message"
    )
    stream = StreamKeyBuilder().user_inbox("Sean")
    print(f"[INJECT] Publishing to stream: {stream}")
    await publish_envelope(redis, stream, env)
    print("[INJECT] Message published.")

if __name__ == "__main__":
    asyncio.run(inject_message())
