import asyncio
import os
from redis.asyncio import Redis

from AG1_AetherBus.bus import publish_envelope, subscribe, build_redis_url
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

from .crypto_utils import apply_signature, verify_envelope
from .ledger import Ledger

SECRET = os.getenv("ATTEST_SECRET", "changeme")
ledger = Ledger()
keys = StreamKeyBuilder()

async def publish_with_attestation(redis: Redis, channel: str, env: Envelope) -> None:
    apply_signature(env, SECRET)
    ledger.record(env.envelope_id, env.agent_name or env.user_id or "unknown", env.auth_signature or "", env.timestamp)
    await publish_envelope(redis, channel, env)

async def subscribe_with_attestation(redis: Redis, channel: str, callback, group: str = "attest", consumer: str | None = None) -> None:
    async def verified(env: Envelope):
        if verify_envelope(env, SECRET) and ledger.verify(env.envelope_id, env.auth_signature or ""):
            await callback(env)
        else:
            print(f"[ATTN] Invalid signature for {env.envelope_id}")
    await subscribe(redis, channel, verified, group=group, consumer=consumer)

async def demo() -> None:
    redis = Redis.from_url(build_redis_url())
    test_stream = keys.agent_inbox("demo-agent")

    async def handler(env: Envelope):
        print(f"[ATTN-DEMO] Received: {env.content}")

    await subscribe_with_attestation(redis, test_stream, handler)

if __name__ == "__main__":
    asyncio.run(demo())
