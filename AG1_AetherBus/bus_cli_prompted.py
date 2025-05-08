import asyncio
import uuid
import json
import os
from redis.asyncio import Redis
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus import publish_envelope, subscribe, build_redis_url
import contextlib

async def wait_for_reply(redis, reply_to, correlation_id, timeout=5):
    q = asyncio.Queue()

    async def handler(env: Envelope):
        if env.correlation_id == correlation_id:
            await q.put(env)

    task = asyncio.create_task(subscribe(redis, reply_to, handler))
    try:
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

async def main():
    print("🔁 Bus CLI Interactive Mode")
    agent = input("Agent name: ").strip()
    user = input("User ID: ").strip()
    session = input("Session code (leave blank if none): ").strip() or None
    text = input("Message text: ").strip()
    wait = input("Wait for reply? (y/n): ").strip().lower() == "y"

    redis = Redis.from_url(build_redis_url())
    keys = StreamKeyBuilder()

    reply_to = keys.user_inbox(user)
    correlation_id = str(uuid.uuid4())

    envelope = Envelope(
        role="user",
        content={"text": text},
        user_id=user,
        agent_name=agent,
        session_code=session,
        reply_to=reply_to,
        correlation_id=correlation_id
    )

    target_stream = keys.agent_inbox(agent) if not session else keys.flow_input(session)

    print(f"[BUS_CLI] Sending to: {target_stream}")
    print(f"[BUS_CLI] Envelope:\n{json.dumps(envelope.to_dict(), indent=2)}")

    await publish_envelope(redis, target_stream, envelope)

    if wait:
        print(f"[BUS_CLI] Waiting on: {reply_to}")
        reply = await wait_for_reply(redis, reply_to, correlation_id)
        if reply:
            print(f"[BUS_CLI] Reply received:\n{json.dumps(reply.to_dict(), indent=2)}")
        else:
            print("[BUS_CLI] No reply received within timeout.")

if __name__ == "__main__":
    asyncio.run(main())
