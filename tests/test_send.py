"""
test_send.py

Purpose:
    Publishes a test Envelope message as a user to an agent's inbox stream on the AG1 core bus.

Usage:
    $ python test_send.py

Expected Output:
    - Prints the stream being published to and a confirmation message.
    - On success, a Redis key like 'AG1:agent:pa0:inbox' will be created/updated.

Verifying:
    Use `redis-cli` or your Redis UI to check for the presence of the key and message.

Note:
    - Requires a running Redis instance and correct core bus configuration.
    - The message is sent to agent_name='pa0', user_id='test-user'; edit as needed for other agents/users.
"""

# test_send.py
import asyncio
from redis.asyncio import Redis
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus import publish_envelope, build_redis_url

async def main():
    redis = Redis.from_url(build_redis_url())

    keys = StreamKeyBuilder()
    env = Envelope(
        role="user",
        agent_name="pa0",
        user_id="test-user",
        session_code="session123",
        content={"command": "echo", "message": "Hello from test_send!"}
    )

    stream = keys.agent_inbox("pa0")
    print(f"📤 Publishing to: {stream}")
    await publish_envelope(redis, stream, env)
    print("✅ Message sent.")

    await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
