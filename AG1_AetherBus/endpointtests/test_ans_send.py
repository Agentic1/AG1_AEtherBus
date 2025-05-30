"""
Test script to publish an ANS search envelope to the bus and print the results.
- Sends an envelope of type 'ANS' to the ans_search agent's inbox
- Listens for a response on a temporary outbox stream
"""
import asyncio
import json 
from pathlib import Path
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus import publish_envelope, build_redis_url
import sys

import asyncio
import uuid
import os
from dotenv import load_dotenv



from redis.asyncio import Redis

# Load environment variables from .env if present (for local dev)
load_dotenv()
# AG1_AetherBus and Redis() will use REDIS_HOST, REDIS_PORT, etc. from env

async def main():
    # Redis() will use environment variables for connection config (see AG1_AetherBus docs)
    redis = Redis.from_url(build_redis_url())
    # Unique reply stream for this test run
    reply_to = f"test.ans_search.outbox.{uuid.uuid4().hex[:8]}"
    # Compose the search envelope
    env = Envelope(
        role="test",
        envelope_type="ANS",
        content={"query": "haka"},  # Change query as needed
        reply_to=reply_to,
        session_code="test-ans-search"
    )
    # Publish the envelope to the ans_search agent's inbox
    inbox = "AG1:agent:ans_search:inbox"
    await publish_envelope(redis, inbox, env)
    print(f"Published ANS search envelope to {inbox}, waiting for reply on {reply_to}...")

    # Wait for a response (timeout after 5 seconds)
    try:
        response = await redis.xread({reply_to: '0'}, count=1, block=5000)
        if response:
            _, messages = response[0]
            for msg_id, msg in messages:
                print("Received ANS_RESULT envelope:")
                print(msg)
        else:
            print("No response received within timeout.")
    finally:
        await redis.delete(reply_to)
        await redis.aclose()  # Use aclose() to avoid deprecation warning

if __name__ == "__main__":
    asyncio.run(main())

    
