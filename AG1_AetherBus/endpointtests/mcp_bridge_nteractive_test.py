# mcp_bridge_interactive_test.py

import asyncio
import json
import os
import uuid
import sys
import traceback 
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
TEST_CLIENT_ID_PREFIX = "interactive_test_client"

# --- Configuration Loading for the Test Client ---
CLIENT_CONFIG = {}
try:
    config_path = os.path.join(current_dir, "test_bridge_config.json")
    with open(config_path, 'r') as f: 
        CLIENT_CONFIG = json.load(f)
    print(f"Successfully loaded client configuration from {config_path}.")
except FileNotFoundError:
    print(f"ERROR: Configuration file {config_path} not found. Create it with your API keys.")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: Configuration file {config_path} is not valid JSON: {e}")
    sys.exit(1)
# --- End Configuration Loading ---

def determine_server_url_config_mapping(
    connection_url_config_schema: dict | None, 
    server_qname: str, # For logging/fallback
    global_creds_config: dict
) -> dict:
    """
    Determines the server_url_config_key_mapping based on the provided
    connection_url_config_schema from the tool blueprint.

    The keys of the returned dict are what the MCP server expects in its URL config.
    The values of the returned dict are the keys in `global_creds_config` (test_bridge_config.json).
    """
    mapping = {}
    
    if connection_url_config_schema and isinstance(connection_url_config_schema, dict):
        print(f"Using connection_url_config_schema for '{server_qname}': {json.dumps(connection_url_config_schema)}")
        
        # The schema from smithery.yaml is expected to be like:
        # "configSchema": {
        #   "type": "object",
        #   "properties": {
        #     "etherscanApiKey": { "type": "string", "description": "...", "x-suggested-client-config-key": "ETHERSCAN_API_KEY" },
        #     "anotherKey": { "type": "string", "description": "...", "x-suggested-client-config-key": "ANOTHER_KEY_IN_CONFIG" }
        #   },
        #   "required": ["etherscanApiKey"]
        # }
        # We are interested in the keys under "properties".

        schema_properties = connection_url_config_schema.get("properties", {})
        if not schema_properties and connection_url_config_schema: # If schema is flat (less likely from smithery.yaml but possible)
            schema_properties = connection_url_config_schema # Treat the whole schema as properties

        for server_expects_key, prop_details in schema_properties.items():
            if not isinstance(prop_details, dict):
                print(f"Warning: Property details for '{server_expects_key}' in schema is not a dict. Skipping.")
                continue

            client_config_key_name = prop_details.get("x-suggested-client-config-key")
            
            # If no explicit suggestion, try some common variations
            if not client_config_key_name:
                # Convert camelCase (server_expects_key) to SNAKE_UPPER_CASE for common env var style
                # e.g., etherscanApiKey -> ETHERSCAN_API_KEY
                s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', server_expects_key)
                client_config_key_name_upper = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).upper()
                
                if client_config_key_name_upper in global_creds_config:
                    client_config_key_name = client_config_key_name_upper
                elif server_expects_key in global_creds_config: # Try direct match
                     client_config_key_name = server_expects_key
                else:
                    print(f"Warning: No 'x-suggested-client-config-key' for '{server_expects_key}' and "
                          f"could not auto-find a match like '{client_config_key_name_upper}' or '{server_expects_key}' in client config.")
                    # We could prompt the user here as an advanced option, or just skip.
                    # For now, skip if no clear match.
                    continue
            
            if client_config_key_name in global_creds_config:
                mapping[server_expects_key] = client_config_key_name
            else:
                print(f"Warning: Suggested client config key '{client_config_key_name}' (for server key '{server_expects_key}') "
                      "not found in client configuration (test_bridge_config.json).")
    else:
        # Fallback to old name-based heuristics if no schema provided by the bridge
        print(f"No connection_url_config_schema provided by bridge for '{server_qname}'. "
              "Falling back to name-based heuristics (if any).")
        qname_lower = server_qname.lower()
        if "etherscan" in qname_lower:
            if "ETHERSCAN_API_KEY" in global_creds_config:
                mapping["etherscanApiKey"] = "ETHERSCAN_API_KEY"
            else:
                print("WARNING: ETHERSCAN_API_KEY not found in client config for Etherscan server (heuristic).")
        # Add other heuristics here if desired as a last resort
    
    if not mapping:
         print(f"No specific URL config key mapping determined for '{server_qname}'. "
               "Will proceed using only the Smithery profile ID if applicable. "
               "If the server requires other specific API keys in its URL config, execution may fail or be limited.")
    return mapping


async def interactive_tool_tester(adapter: BusAdapterV2, global_creds_config: dict):
    print("\n--- Interactive MCP Tool Tester (via AetherBus Bridge) ---")

    # Credentials for the bridge to use when calling Smithery helper services (Toolbox, Fetch)
    # These are passed in the discovery request.
    smithery_gateway_api_key_for_bridge_helpers = global_creds_config.get("SMITHERY_GATEWAY_API_KEY")
    smithery_profile_id_for_bridge_helpers = global_creds_config.get("SMITHERY_GATEWAY_PROFILE_ID")
    
    if not smithery_gateway_api_key_for_bridge_helpers or not smithery_profile_id_for_bridge_helpers:
        print("ERROR: Smithery Gateway API Key or Profile ID not in client config. These are needed by the bridge. Exiting.")
        return

    # --- 1. Get Discovery Query from User ---
    general_discovery_query = input("Enter a general search query for MCP servers (e.g., 'twitter', 'etherscan', 'duckduckgo', or a qualifiedName): ").strip()
    if not general_discovery_query:
        print("No search query entered. Exiting."); return
    
    max_results_str = input("Max discovery results to show (e.g., 5, press Enter for default 5): ").strip()
    max_results = int(max_results_str) if max_results_str.isdigit() else 5

    # --- 2. Send Discovery Request to Bridge ---
    print(f"\nRequesting discovery for query: '{general_discovery_query}' from bridge...")
    # The client sends its Smithery credentials for the *bridge* to use when calling Smithery services like Toolbox or Fetch.
    # It also sends its own collection of service API keys so the bridge can (optionally) note if the agent has the required keys.
    discovery_request_content = {
        "action": "discover_mcp_tools", # This action now triggers the enriched blueprint generation in the bridge
        "discovery_target": {
            "type": "smithery_toolbox_search",
            "toolbox_mcp_url_base": "server.smithery.ai/@smithery/toolbox", 
            "search_query": general_discovery_query, 
            "n": max_results 
        },
        # Credentials for the BRIDGE to use for its helper calls (e.g., to Smithery Toolbox, @smithery-ai/fetch)
        "smithery_gateway_api_key": smithery_gateway_api_key_for_bridge_helpers,
        "smithery_profile_id": smithery_profile_id_for_bridge_helpers,
        # Agent's own collection of API keys (the bridge can use this for informational notes)
        "service_api_keys": global_creds_config # Pass the whole client config for the bridge to reference
    }
    request_env_discovery = Envelope(role="Agent",content=discovery_request_content, agent_name=adapter.agent_id)
    
    try:
        # Increased timeout as bridge now does more work (GitHub fetches)
        response_env_discovery = await adapter.request_response(MCP_BRIDGE_INBOX, request_env_discovery, timeout=90.0) 
    except asyncio.TimeoutError:
        print("ERROR: Discovery request to bridge timed out (bridge might be fetching from GitHub)."); return
    except Exception as e:
        print(f"ERROR: During discovery request to bridge: {type(e).__name__} - {e}"); traceback.print_exc(); return

    if not response_env_discovery or not response_env_discovery.content:
        print("ERROR: No valid response content from bridge for discovery."); return

    discovery_result = response_env_discovery.content
    print("--- Bridge Discovery Response (Tool Blueprints) ---"); print(json.dumps(discovery_result, indent=2))

    if discovery_result.get("status") != "success" or not discovery_result.get("tool_blueprints"):
        print(f"Discovery via bridge failed or no tool_blueprints returned. Error: {discovery_result.get('error')}"); return
    
    tool_blueprints = discovery_result["tool_blueprints"]
    if not tool_blueprints:
        print(f"No tool blueprints found by bridge for query '{general_discovery_query}'."); return

    # --- 3. User Selects a Tool from the Blueprints ---
    # The blueprints are now a flat list of tools, each with server info.
    print("\nDiscovered Tool Blueprints:")
    for i, bp in enumerate(tool_blueprints):
        print(f"  {i+1}. Server: {bp.get('server_qualified_name')} ({bp.get('server_display_name')})")
        print(f"      Tool:   {bp.get('tool_name')} - {bp.get('tool_description', 'No description')}")
        print(f"      Notes:  {bp.get('connection_config_notes', 'N/A')}")
        # print(f"      Schema: {json.dumps(bp.get('connection_url_config_schema'))}") # Optional: for debugging
    
    try:
        blueprint_choice_idx = int(input(f"Select a tool blueprint by number (1-{len(tool_blueprints)}): ")) - 1
        if not (0 <= blueprint_choice_idx < len(tool_blueprints)):
            raise ValueError("Invalid blueprint number.")
        selected_blueprint = tool_blueprints[blueprint_choice_idx]
    except ValueError as e:
        print(f"Invalid selection: {e}. Exiting."); return
        
    qname_of_selected_server = selected_blueprint["server_qualified_name"]
    tool_name_to_execute = selected_blueprint["tool_name"]
    print(f"\nYou selected Server: {qname_of_selected_server}, Tool: {tool_name_to_execute}")

    # --- 4. User Provides Tool Arguments (using tool_input_schema from blueprint) ---
    tool_input_schema = selected_blueprint.get("tool_input_schema", {})
    print("\nTool Input Schema for selected tool:")
    print(json.dumps(tool_input_schema, indent=2))
    
    tool_execution_args = {}
    schema_properties = tool_input_schema.get("properties", {})
    required_args = tool_input_schema.get("required", []) # Smithery.yaml seems to put 'required' at top level of inputSchema
    if not required_args and isinstance(tool_input_schema.get("additionalProperties"), dict):
         required_args = tool_input_schema.get("additionalProperties", {}).get("required", [])

    for prop_name, prop_schema in schema_properties.items():
        is_required = prop_name in required_args
        prompt_msg = f"Enter value for '{prop_name}' ({prop_schema.get('type', 'any')})"
        if is_required: prompt_msg += " (required)"
        else: prompt_msg += f" (optional, default: {prop_schema.get('default', 'None')})"
        prompt_msg += ": "
        
        user_val_str = input(prompt_msg).strip()
        
        if user_val_str:
            prop_type = prop_schema.get("type")
            try:
                if prop_type == "number": tool_execution_args[prop_name] = float(user_val_str)
                elif prop_type == "integer": tool_execution_args[prop_name] = int(user_val_str)
                elif prop_type == "boolean": tool_execution_args[prop_name] = user_val_str.lower() in ['true', '1', 't', 'y', 'yes']
                elif prop_type == "array":
                    array_items = [item.strip() for item in user_val_str.split(',')]
                    item_schema = prop_schema.get("items", {})
                    item_type = item_schema.get("type")
                    if item_type:
                        converted_items = []
                        for item_str in array_items:
                            try:
                                if item_type == "number": converted_items.append(float(item_str))
                                elif item_type == "integer": converted_items.append(int(item_str))
                                elif item_type == "boolean": converted_items.append(item_str.lower() in ['true', '1', 't', 'y', 'yes'])
                                else: converted_items.append(item_str) 
                            except ValueError:
                                print(f"Warning: Could not convert array item '{item_str}' to {item_type} for '{prop_name}'. Adding as string.")
                                converted_items.append(item_str)
                        tool_execution_args[prop_name] = converted_items
                    else:
                        tool_execution_args[prop_name] = array_items 
                else: tool_execution_args[prop_name] = user_val_str 
            except ValueError:
                print(f"Warning: Could not convert '{user_val_str}' to {prop_type} for '{prop_name}'. Sending as string.")
                tool_execution_args[prop_name] = user_val_str
        elif is_required:
            print(f"ERROR: Required argument '{prop_name}' not provided. Exiting."); return

    # --- 5. AUTOMATICALLY Determine Server URL Config Key Mapping (using connection_url_config_schema) ---
    print("\n--- Server Connection Configuration (for the TARGET MCP server's URL) ---")
    connection_url_config_schema_from_bp = selected_blueprint.get("connection_url_config_schema")
    
    server_url_config_key_mapping = determine_server_url_config_mapping(
        connection_url_config_schema_from_bp,
        qname_of_selected_server,
        global_creds_config 
    )
    
    if server_url_config_key_mapping:
        print(f"Automatically determined mapping of (Server Expected Key -> Client Config Key): {json.dumps(server_url_config_key_mapping, indent=2)}")
    # (determine_server_url_config_mapping already prints detailed messages)

    # --- 6. Prepare Final Configs and Send Execution Request to Bridge ---
    # This is the config that will be base64 encoded for the TARGET server's URL 'config' parameter.
    # It always includes the Smithery profile (if the server is Smithery-hosted) 
    # and any server-specific keys determined in step 5.
    
    # The profile ID used here should be the one the *agent* wants to use for the *target service*.
    # This might be the same as smithery_profile_id_for_bridge_helpers, or different if the agent has multiple profiles.
    # For simplicity in this test client, we'll use the same one.
    agent_profile_for_target_service = global_creds_config.get("SMITHERY_GATEWAY_PROFILE_ID")

    mcp_url_config_for_target_execution = {"dynamic": False, "profile": agent_profile_for_target_service}
    
    for server_expected_key, key_in_global_creds in server_url_config_key_mapping.items():
        value = global_creds_config.get(key_in_global_creds)
        # Warning for missing value should have been handled by determine_server_url_config_mapping
        mcp_url_config_for_target_execution[server_expected_key] = value 
    
    # The api_key_for_gateway is the Smithery Gateway API Key, used to authenticate the bridge's call TO Smithery Gateway
    # when it executes the *target* service. This is typically the agent's (or this test client's) main Smithery API key.
    api_key_for_target_execution_via_gateway = global_creds_config.get("SMITHERY_GATEWAY_API_KEY")

    execution_request_content = {
        "action": "execute_mcp_tool",
        "protocol": selected_blueprint.get("protocol_preference", "streamablehttp"), # Use preference from blueprint
        "endpoint_id": qname_of_selected_server,
        "tool_name": tool_name_to_execute,
        "tool_args": tool_execution_args,
        "mcp_url_config": mcp_url_config_for_target_execution, 
        "api_key_for_gateway": api_key_for_target_execution_via_gateway 
    }
    request_env_execution = Envelope(role="Agent",content=execution_request_content, agent_name=adapter.agent_id)

    print(f"\nSending execution request to bridge for {qname_of_selected_server}/{tool_name_to_execute}...")
    print(f"  Target Server's MCP URL Config (to be base64 encoded in URL): {json.dumps(mcp_url_config_for_target_execution, indent=2)}")
    print(f"  Tool Args for target: {json.dumps(tool_execution_args, indent=2)}")
    print(f"  API Key for Smithery Gateway (for this execution): {'******' if api_key_for_target_execution_via_gateway else 'Not Set'}")

    try:
        response_env_execution = await adapter.request_response(MCP_BRIDGE_INBOX, request_env_execution, timeout=60.0)
    except asyncio.TimeoutError:
        print("ERROR: Execution request to bridge timed out."); return
    except Exception as e:
        print(f"ERROR: During execution request to bridge: {type(e).__name__} - {e}"); traceback.print_exc(); return

    if not response_env_execution or not response_env_execution.content:
        print("ERROR: No valid response content from bridge for execution."); return

    execution_result = response_env_execution.content
    print(f"--- Bridge Execution Response for {qname_of_selected_server}/{tool_name_to_execute} ---")
    print(json.dumps(execution_result, indent=2))

    if execution_result.get("status") == "success":
        print("\nSUCCESS: Tool executed successfully via bridge!")
    else:
        print("\nFAILURE: Tool execution via bridge failed.")
        print(f"  Error details: {execution_result.get('error')}")
        if execution_result.get('details'):
            print(f"  Traceback/Details: {execution_result.get('details')}")


async def main_interactive_test_runner():
    if not CLIENT_CONFIG: 
        print("Client configuration (test_bridge_config.json) not loaded or empty. Cannot run tests.")
        return

    redis_client = None
    try:
        redis_url = build_redis_url()
        redis_client = await aioredis.from_url(redis_url, decode_responses=True) 
        await redis_client.ping() 
        print(f"Successfully connected to Redis at {redis_url}")
    except Exception as e:
        print(f"ERROR: Could not connect to Redis: {e}")
        if redis_client: await redis_client.aclose()
        return
    
    adapter = None
    try:
        async def dummy_core_handler(env, redis): pass 
        adapter = BusAdapterV2(f"{TEST_CLIENT_ID_PREFIX}:{uuid.uuid4().hex}", dummy_core_handler, redis_client)
        await interactive_tool_tester(adapter, CLIENT_CONFIG)
    except Exception as e: 
        print(f"FATAL ERROR during interactive test execution: {type(e).__name__} - {e}")
        traceback.print_exc()
    finally:
        if redis_client:
            print("Closing Redis connection for test client...")
            await asyncio.sleep(0.2) 
            await redis_client.aclose()
            print("Test client Redis connection closed.")

if __name__ == "__main__":
    print("Starting Interactive MCP Bridge Test Client...")
    asyncio.run(main_interactive_test_runner())