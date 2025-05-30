# simple_test_bridge.py
import asyncio
import json
import uuid
import os

from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus import publish_envelope, subscribe, build_redis_url
from redis.asyncio import Redis

# Channel your mcp_bridge_gem.py is LISTENING ON
BRIDGE_INBOX = "AG1:edge:mcp:main:inbox" 

# Where THIS SCRIPT will listen for the reply from the bridge
MY_REPLY_STREAM = f"test_client:reply_to_me:{str(uuid.uuid4())[:8]}"

# --- Envelope Content to Send to the Bridge ---
# Calling healthcare-mcp-public's "pubmed_search" tool
LOCAL_HEALTHCARE_BASE_URL = "http://localhost:8000" # Where healthcare-mcp-public runs

envelope_content_for_bridge = {
    "endpoint": LOCAL_HEALTHCARE_BASE_URL, # Bridge's make_mcp_sse_url will append /mcp/sse
    "config": {}, # For MCP session with healthcare server.
    "tool": "pubmed_search",
    "args": {
        "query": "lions mane mushroom benefits",
        "max_results": 1
    },
    "api_key": "" # For Smithery Gateway; bridge's make_mcp_sse_url should handle empty for local.
}

async def listen_for_reply(redis_client, stop_event):
    print(f"Test script listening for reply on: {MY_REPLY_STREAM}")
    
    async def handle_reply_envelope(env: Envelope):
        print("\n--- Test Script Received Reply ---")
        print(json.dumps(env.to_dict(), indent=2))
        print("--- End of Reply ---")
        stop_event.set() # Signal that we got the reply

    # Need to ensure the group/consumer for this test subscription is unique or managed
    # For a simple test, a unique group name is easiest.
    await subscribe(redis_client, MY_REPLY_STREAM, handle_reply_envelope, 
                    group=f"test_client_group_{str(uuid.uuid4())[:4]}", 
                    consumer="test_client_consumer")
    print("Test script subscription ended.")


async def main():
    print("Starting Simple Python Test for MCP Bridge...")
    redis_url = build_redis_url()
    print(f"Connecting to Redis at: {redis_url}")
    redis_client = await Redis.from_url(redis_url)
    await redis_client.ping()
    print("Successfully connected to Redis.")

    request_envelope = Envelope(
        envelope_id=f"simple_test_{str(uuid.uuid4())}",
        role="simple_test_client",
        content=envelope_content_for_bridge,
        reply_to=MY_REPLY_STREAM, # Bridge will send result here
        correlation_id=str(uuid.uuid4())
    )

    # Event to signal the listener to stop once a reply is received
    stop_listening_event = asyncio.Event()

    # Start the listener in the background
    listener_task = asyncio.create_task(listen_for_reply(redis_client, stop_listening_event))
    
    # Give the listener a moment to subscribe
    await asyncio.sleep(1) 

    print(f"\nPublishing request to bridge on: {BRIDGE_INBOX}")
    print("Envelope content being sent to bridge:")
    print(json.dumps(request_envelope.content, indent=2))

    await publish_envelope(redis_client, BRIDGE_INBOX, request_envelope)
    print(f"Successfully published request envelope ID: {request_envelope.envelope_id}")

    print("Waiting for reply or timeout...")
    try:
        await asyncio.wait_for(stop_listening_event.wait(), timeout=30.0) # Wait 30s for reply
        print("Reply received by test script.")
    except asyncio.TimeoutError:
        print("Timeout: No reply received by test script within 30 seconds.")
    finally:
        listener_task.cancel() # Stop the listener task
        try:
            await listener_task
        except asyncio.CancelledError:
            print("Listener task cancelled.")
        await redis_client.aclose()
        print("Redis connection closed by test script.")

if __name__ == "__main__":
    asyncio.run(main())