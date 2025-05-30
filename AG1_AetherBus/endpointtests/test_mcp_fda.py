import asyncio
import json
import uuid
import os

# Adjust import paths if necessary
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus import publish_envelope, build_redis_url
from redis.asyncio import Redis

TARGET_STREAM = "AG1:edge:mcp:main:inbox"

# --- Configuration for the test ---
LOCAL_HEALTHCARE_SERVER_BASE_URL = "http://localhost:8000" # Where healthcare-mcp-public runs

# --- Envelope Content to Send ---
envelope_content = {
    "endpoint": LOCAL_HEALTHCARE_SERVER_BASE_URL, # Bridge will form http://localhost:8000/mcp/sse
    "config": {}, # For the MCP Session with healthcare server.
                  # fda_drug_lookup tool gets FDA_API_KEY from healthcare server's env.
    "tool": "fda_drug_lookup",
    "args": {
        "drug_name": "ivermectin",
        "search_type": "general" # Or "label" or "adverse_events"
    },
    "api_key": "" # For Smithery Gateway; not used for local connection to healthcare server.
                  # Bridge's make_mcp_sse_url should handle empty/None for local.
}

async def main():
    print("Starting Python test publisher for Healthcare MCP (fda_drug_lookup)...")
    redis_url = build_redis_url()
    print(f"Connecting to Redis at: {redis_url}")

    try:
        redis_client = await Redis.from_url(redis_url)
        await redis_client.ping()
        print("Successfully connected to Redis.")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")
        return

    test_envelope = Envelope(
        envelope_id=f"py_test_healthcare_fda_{str(uuid.uuid4())}",
        role="python_test_script_fda",
        user_id="test_script_user_fda",
        agent_name="python_test_publisher_fda",
        content=envelope_content,
        reply_to="my_agent_healthcare_fda_replies", # Expect result here
        correlation_id=str(uuid.uuid4()),
        envelope_type="mcp_request_healthcare_fda",
        meta={"source": "test_publish_mcp_request.py"}
    )

    print(f"\nPublishing to stream: {TARGET_STREAM}")
    print("Envelope content being sent:")
    print(json.dumps(test_envelope.content, indent=2))

    try:
        await publish_envelope(redis_client, TARGET_STREAM, test_envelope)
        print(f"\nSuccessfully published envelope with ID: {test_envelope.envelope_id} to {TARGET_STREAM}")
        print("Check your mcp_bridge_gem.py logs and the healthcare server logs.")
        print("Check Redis stream 'my_agent_healthcare_fda_replies' for the result.")
    except Exception as e:
        print(f"Error during publish_envelope: {e}")
    finally:
        await redis_client.aclose()
        print("Redis connection closed.")

if __name__ == "__main__":
    asyncio.run(main())