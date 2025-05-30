import asyncio
import json
import uuid
import os

from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus import publish_envelope, build_redis_url
from redis.asyncio import Redis

TARGET_STREAM = "AG1:edge:mcp:main:inbox"

# --- Define the arguments for "@smithery/toolbox"'s "use_tool" ---
# This structure now nests all actual parameters for use_tool under a "parameters" key
args_for_use_tool = {
    "parameters": { # Top-level key expected by use_tool
        "qualifiedName": "@simonfraserduncan/echo-mcp",
        "name": "echo", # Tool on target
        "args": {        # Args for target tool
            "message": "Hello Simon Echo via Toolbox (V5 structure)"
        },
        "mcpConfig": {},
        "smitheryApiKey": "52b27945-63ec-You424b-a405-efbc372dd3b6"
    }
}


# In test_publish_mcp_request.py
LOCAL_HEALTHCARE_BASE_URL = "http://localhost:8000" # Where healthcare-mcp-public runs

envelope_content = {
    "endpoint": LOCAL_HEALTHCARE_BASE_URL, # The make_mcp_sse_url will append /mcp/sse
    "config": {}, # This 'config' is for the MCP session, healthcare server likely doesn't need one.
    "tool": "pubmed_search", # An actual tool from healthcare-mcp-public
    "args": {
        "query": "Ivermectin for use in cancer patients",
        "max_results": 1
        # "date_range": "1" # Optional arg for pubmed_search
    },
    "api_key": "" # For Smithery Gateway; pass empty or None for local connections.
                  # make_mcp_sse_url should not append &api_key= if this is empty/None for local.
}


# --- Define the full content for the Envelope ---
xenvelope_content = {
    "endpoint": "https://server.smithery.ai/@smithery/toolbox/mcp",
    "config": {},
    "tool": "use_tool",
    "args": args_for_use_tool, # The structured arguments for "use_tool"
    "api_key": "52b27945-63ec-424b-a405-efbc372dd3b6"
}

async def main():
    print("Starting Python test publisher (V5 - Corrected use_tool nesting)...")
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
        envelope_id=f"py_test_toolbox_sfd_echo_v5_{str(uuid.uuid4())}",
        role="python_test_script_agent_v5",
        user_id="test_script_user_v5",
        agent_name="python_test_publisher_v5",
        content=envelope_content,
        reply_to="my_agent_python_test_sfd_echo_v5_replies",
        correlation_id=str(uuid.uuid4()),
        envelope_type="mcp_metatool_sfd_echo_py_v5",
        meta={"source": "test_publish_mcp_request_v5.py"}
    )

    print(f"\nPublishing to stream: {TARGET_STREAM}")
    print("Envelope content.args being sent (these are the args for 'use_tool'):")
    print(json.dumps(test_envelope.content['args'], indent=2))

    try:
        await publish_envelope(redis_client, TARGET_STREAM, test_envelope)
        print(f"\nSuccessfully published envelope with ID: {test_envelope.envelope_id} to {TARGET_STREAM}")
        print("Check your mcp_bridge_gem.py logs and the reply stream: my_agent_python_test_sfd_echo_v5_replies")
    except Exception as e:
        print(f"Error during publish_envelope: {e}")
    finally:
        await redis_client.aclose()
        print("Redis connection closed.")

if __name__ == "__main__":
    asyncio.run(main())