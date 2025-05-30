# test_bridge_client.py
import asyncio
import json
import os
import uuid
import sys
import traceback # For detailed error printing
import re

# --- Add project root to sys.path ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root_AetherBus = os.path.join(current_dir, "..") 
if project_root_AetherBus not in sys.path:
    sys.path.insert(0, project_root_AetherBus)
# --- End sys.path modification ---

from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus import build_redis_url
from AG1_AetherBus.bus_adapterV2 import BusAdapterV2
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv() 

MCP_BRIDGE_INBOX = os.getenv("MCP_BRIDGE_INBOX", "AG1:edge:mcp:main:inbox")
TEST_CLIENT_ID = f"test_client_for_bridge:{uuid.uuid4().hex}"

# Load credentials from a config file for the test client
TEST_CONFIG = {}
try:
    with open("test_bridge_config.json", 'r') as f: # Same config file as before
        TEST_CONFIG = json.load(f)
except Exception as e:
    print(f"Error loading test_bridge_config.json: {e}. Some tests may fail.")

async def test_discovery_service(adapter: BusAdapterV2):
    print("\n--- Testing Discovery Service via AetherBus ---")
    smithery_gw_key = TEST_CONFIG.get("SMITHERY_GATEWAY_API_KEY")
    smithery_profile = TEST_CONFIG.get("SMITHERY_GATEWAY_PROFILE_ID")

    if not smithery_gw_key or not smithery_profile:
        print("ERROR: Smithery Gateway credentials not in test_bridge_config.json for discovery test.")
        return

    discovery_request_content = {
        "action": "discover_mcp_tools",
        "discovery_target": {
            "type": "smithery_toolbox_search",
            "toolbox_mcp_url_base": "server.smithery.ai/@smithery/toolbox",
            "search_query": "duckduckgo", 
            "n": 1 
        },
        "mcp_session_config_for_toolbox": { 
            "profile": smithery_profile, "dynamic": False
        },
        "api_key_for_toolbox": smithery_gw_key,
        "smithery_profile_id": smithery_profile # Make sure this is included
    }
    xdiscovery_request_content = {
        "action": "discover_mcp_tools", # Or "get_enriched_tool_blueprints"
        # ...
        "smithery_profile_id": smithery_profile # Make sure this is included
    }
    print(f"[TestClient] Sending discovery request content: {json.dumps(discovery_request_content, indent=2)}")
    request_envelope = Envelope(role="Agent", content=discovery_request_content, agent_name=TEST_CLIENT_ID)

    try:
        response_envelope = await adapter.request_response(MCP_BRIDGE_INBOX, request_envelope, timeout=30.0)
        if response_envelope:
            print("[TestClient] Discovery Response Content:")
            print(json.dumps(response_envelope.content, indent=2))
            assert response_envelope.content.get("status") == "success"
            assert "tool_blueprints" in response_envelope.content
            blueprints = response_envelope.content["tool_blueprints"] # Use the correct key
            assert blueprints is not None, "Response missing 'tool_blueprints'"
            assert len(blueprints) > 0
            print("[TestClient] Discovery Service Test: PASSED")
        else:
            print("[TestClient] Discovery Service Test: FAILED (Timeout or no response)")
    except Exception as e:
        print(f"[TestClient] Discovery Service Test: FAILED - {type(e).__name__}: {e}")
        traceback.print_exc()

async def test_execution_service_etherscan(adapter: BusAdapterV2):
    print("\n--- Testing Execution Service (Etherscan check-balance) via AetherBus ---")
    smithery_gw_key = TEST_CONFIG.get("SMITHERY_GATEWAY_API_KEY")
    smithery_profile = TEST_CONFIG.get("SMITHERY_GATEWAY_PROFILE_ID")
    etherscan_key = TEST_CONFIG.get("ETHERSCAN_API_KEY")

    if not smithery_gw_key or not smithery_profile or not etherscan_key:
        print("ERROR: Smithery or Etherscan credentials not in test_bridge_config.json.")
        return

    etherscan_service_url_config = {
        "dynamic": False, 
        "profile": smithery_profile,
        "etherscanApiKey": etherscan_key 
    }
    execution_request_content = {
        "action": "execute_mcp_tool",
        "protocol": "streamablehttp",
        "endpoint_id": "@nickclyde/duckduckgo-mcp-server", #@ThirdGuard/mcp-etherscan-server
        "tool_name": "search", #check-balance
        "tool_args": {"query": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"},
        "mcp_url_config": etherscan_service_url_config,
        "api_key_for_gateway": smithery_gw_key
    }
    request_envelope = Envelope(role="Agent",content=execution_request_content, agent_name=TEST_CLIENT_ID)

    try:
        response_envelope = await adapter.request_response(MCP_BRIDGE_INBOX, request_envelope, timeout=30.0)
        if response_envelope:
            print("[TestClient] Etherscan Execution Response Content:")
            print(json.dumps(response_envelope.content, indent=2))
            assert response_envelope.content.get("status") == "success"
            assert "result" in response_envelope.content
            assert "Balance" in response_envelope.content["result"]["content"][0]["text"]
            print("[TestClient] Etherscan Execution Test: PASSED")
        else:
            print("[TestClient] Etherscan Execution Test: FAILED (Timeout or no response)")
    except Exception as e:
        print(f"[TestClient] Etherscan Execution Test: FAILED - {type(e).__name__}: {e}")
        traceback.print_exc()

async def main():
    redis_url = build_redis_url()
    redis_client = await aioredis.from_url(redis_url, decode_responses=True)
    
    async def dummy_core_handler(env, redis): pass # Not used by this client
    adapter = BusAdapterV2(TEST_CLIENT_ID, dummy_core_handler, redis_client)
    # No need to await adapter.start() if only using request_response

    await test_discovery_service(adapter)
    print(f'\n------------------------------------------------------------\n[Adapter] {adapter}\n\n')
    await test_execution_service_etherscan(adapter) # Add this test

    await asyncio.sleep(0.1) # Small delay
    await redis_client.aclose() # This should now be safe if BusAdapterV2.remove_subscription cancels tasks

if __name__ == "__main__":
    asyncio.run(main())