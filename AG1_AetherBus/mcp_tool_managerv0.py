"""**Simplified ASCII Flow for Tool Setup & Execution within `MuseAgent`:**

```
+---------------------+     +-----------------------+     +---------------------+     +-----------------------+     +----------------------+
| muse_agent_config.json| --> | MuseAgent.__init__    | --> | async_mcp_setup()   | --> | _create_function_tools| --> | MuseAgentIntelligent |
| (tool queries,      |     | (loads mcp_config,    |     | (1. Calls Bridge for|     | (For each blueprint)  |     | (gets list of        |
|  agent API keys,    |     |  agent Smithery creds)|     |    blueprints via   |     |  a. Create specific |     |  FunctionTools)      |
|  bridge inbox)      |     +-----------------------+     |    request_response)|     |     wrapper_func    |     +----------------------+
+---------------------+                                   | (2. Gets list of    |     |     (has clean sig) |
                                                          |    server blueprints|     |  b. This wrapper    |
                                                          +-----------------------+     |     calls           |
                                                                                        |     _actual_mcp_exec|
                                                                                        |  c. Create          |
                                                                                        |     FunctionTool    |
                                                                                        |     (name, desc,    |
                                                                                        |      wrapper_func)  |
                                                                                        +---------------------+
                                                                                                  | (FunctionTool)
                                                                                                  V
                               +-------------------------------------------------------------------+
                               | MuseAgentIntelligent (LLM)                                        |
                               | - Sees tool name & description.                                   |
                               | - Decides to call tool with args.                                 |
                               | - AutoGen invokes the specific wrapper_func.                      |
                               +-------------------------------------------------------------------+
                                                 | (wrapper_func is called with LLM args)
                                                 V
                               +--------------------------------------+
                               | _actual_mcp_executor() in MuseAgent  |
                               | - Receives blueprint & specific args.|
                               | - Constructs mcp_url_config.         |
                               | - Constructs bridge request envelope.|
                               | - Calls Bridge via request_response. |
                               | - Returns result/error string.       |
                               +--------------------------------------+
```"""

# AG1_Muse/muse_agent/mcp_tool_manager.py

import asyncio
import json
import re
from typing import List, Dict, Any, Optional
import os

# Assuming these are accessible from this new file's location
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.bus_adapterV2 import BusAdapterV2 # For making request_response calls
from autogen_core.tools import FunctionTool
import functools

# Default MCP Bridge Inbox if not specified in agent's config
MCP_BRIDGE_INBOX_DEFAULT = os.getenv("MCP_BRIDGE_INBOX", "AG1:edge:mcp:main:inbox")


class McpToolFactory:
    def __init__(self, 
                 agent_name: str, 
                 agent_mcp_config: Dict, # Contains bridge_inbox, smithery creds, service_api_keys
                 mcp_bridge_client_adapter: BusAdapterV2
                ):
        self.agent_name = agent_name
        self.mcp_config = agent_mcp_config # Store the dict directly
        self.mcp_bridge_client_adapter = mcp_bridge_client_adapter
        print(f"[McpToolFactory][__init__] Initialized for agent '{self.agent_name}'.")
        print(f"[McpToolFactory][__init__] Using bridge inbox: {self.mcp_config.get('bridge_inbox')}")
        print(f"[McpToolFactory][__init__] Agent Smithery API Key present: {'Yes' if self.mcp_config.get('smithery_api_key') else 'No'}")


    # This is the function whose signature FunctionTool will primarily see
    # after we use functools.partial.
    async def _dynamic_tool_execution_wrapper_core(
        self, # This 'self' will be the McpToolFactory instance, bound by partial
        blueprint_for_this_call: Dict, 
        log_prefix_for_this_call: str,
        # This is the ONLY argument that the LLM/AutoGen should provide:
        tool_arguments: Optional[Dict[str, Any]] = None 
    ):
        # Now, 'self' here refers to the McpToolFactory instance.
        # 'blueprint_for_this_call' and 'log_prefix_for_this_call' are baked in.
        # 'tool_arguments' is what the LLM provides.

        print(f"[McpToolFactory][WrapperCore][{log_prefix_for_this_call}] === ENTERING WRAPPER CORE ===")
        print(f"[McpToolFactory][WrapperCore][{log_prefix_for_this_call}] Blueprint: {blueprint_for_this_call.get('server_qualified_name')}/{blueprint_for_this_call.get('tool_name')}")
        print(f"[McpToolFactory][WrapperCore][{log_prefix_for_this_call}] Raw 'tool_arguments' param received: {json.dumps(tool_arguments)}")

        actual_mcp_args_for_bridge = {}
        if isinstance(tool_arguments, dict):
            # LLM is prompted to send {"tool_arguments": { <actual_params> }}
            # The 'tool_arguments' parameter of this wrapper *is* that outer dict.
            actual_mcp_args_for_bridge = tool_arguments.get("tool_arguments", tool_arguments)
        elif tool_arguments is None and not (blueprint_for_this_call.get("tool_input_schema", {}).get("properties")):
            actual_mcp_args_for_bridge = {}
        else:
            print(f"[McpToolFactory][WrapperCore][{log_prefix_for_this_call}] Warning: tool_arguments unexpected: {tool_arguments}. Using empty.")
        
        print(f"[McpToolFactory][WrapperCore][{log_prefix_for_this_call}] Extracted actual MCP args for executor: {json.dumps(actual_mcp_args_for_bridge)}")

        return await self._actual_mcp_executor( # Call using 'self' from McpToolFactory
            blueprint=blueprint_for_this_call,
            log_prefix_for_executor=log_prefix_for_this_call,
            **actual_mcp_args_for_bridge 
        )

    async def discover_and_build_tools(self) -> List[FunctionTool]:
        """
        Orchestrates discovery of MCP tools via the bridge and builds FunctionTool instances.
        """
        print(f"[McpToolFactory][discover_and_build_tools] Starting MCP tool discovery...")
        all_blueprints = []
        initial_tool_queries = self.mcp_config.get('initial_tool_queries', [])

        if not initial_tool_queries:
            print("[McpToolFactory][discover_and_build_tools] No initial tool queries defined in config.")
            return []

        for search_term in initial_tool_queries:
            print(f"[McpToolFactory][discover_and_build_tools] Discovering tools for query: '{search_term}'")
            discovery_request_content = {
                "action": "discover_mcp_tools",
                "discovery_target": {"type": "smithery_toolbox_search", "search_query": search_term, "n": 1}, # n=5
                "smithery_gateway_api_key": self.mcp_config.get('smithery_api_key'),
                "smithery_profile_id": self.mcp_config.get('smithery_profile_id'),
                "service_api_keys": self.mcp_config.get('service_api_keys', {}) 
            }
            request_env = Envelope(role="Agent", content=discovery_request_content, agent_name=self.agent_name)
            
            try:
                response_env = await self.mcp_bridge_client_adapter.request_response(
                    self.mcp_config.get('bridge_inbox', MCP_BRIDGE_INBOX_DEFAULT), 
                    request_env, 
                    timeout=300.0 
                )
                if response_env and response_env.content and response_env.content.get("status") == "success":
                    blueprints = response_env.content.get("tool_blueprints", [])
                    print(f"[McpToolFactory][discover_and_build_tools] Received {len(blueprints)} tool blueprints for query '{search_term}'.")
                    all_blueprints.extend(blueprints)
                # ... (error logging for failed discovery as in MuseAgent.async_mcp_setup) ...
                elif response_env and response_env.content:
                    error_detail = response_env.content.get('error', 'Unknown error')
                    print(f"[McpToolFactory][discover_and_build_tools] Discovery for '{search_term}' failed. Status: {response_env.content.get('status')}, Error: {str(error_detail)[:500]}...")
                else:
                    print(f"[McpToolFactory][discover_and_build_tools] No valid response from bridge for query '{search_term}'.")
            except asyncio.TimeoutError:
                print(f"[McpToolFactory][discover_and_build_tools] Timeout discovering for '{search_term}'.")
            except Exception as e:
                print(f"[McpToolFactory][discover_and_build_tools] Exception discovering for '{search_term}': {e}")
        
        if all_blueprints:
            return self._create_tools_from_server_blueprints(all_blueprints)
        return []

    def _create_tools_from_server_blueprints(self, tool_blueprints: List[Dict]) -> List[FunctionTool]:
        created_tools: List[FunctionTool] = []
        unique_tool_identifiers = set()
        print(f"[McpToolFactory][_create_tools] Processing {len(tool_blueprints)} blueprints...")

        for bp_data in tool_blueprints:
            # ... (server_qname, tool_name_orig, func_tool_name sanitization as before) ...
            server_qname = bp_data.get('server_qualified_name')
            tool_name_orig = bp_data.get('tool_name')
            if not server_qname or not tool_name_orig: continue
            server_qname_safe = re.sub(r'\W', '_', server_qname.replace('@', 'at_'))
            tool_name_safe = re.sub(r'\W', '_', tool_name_orig)
            func_tool_name = f"mcp_{server_qname_safe}_{tool_name_safe}"
            if func_tool_name in unique_tool_identifiers: continue
            unique_tool_identifiers.add(func_tool_name)
            
            # Use functools.partial to create a callable that FunctionTool will inspect.
            # This callable will have 'self' (McpToolFactory instance), 
            # 'blueprint_for_this_call', and 'log_prefix_for_this_call' pre-filled.
            # The only remaining argument for FunctionTool to see in the signature
            # of the *resulting partial object* (when inspected) will be 'tool_arguments'.
            
            # Create the partial object. FunctionTool will inspect the signature of _dynamic_tool_execution_wrapper_core
            # but with the first three arguments (self, blueprint, log_prefix) already bound.
            executable_for_tool = functools.partial(
                self._dynamic_tool_execution_wrapper_core, # The method of the McpToolFactory instance
                # 'self' is implicitly passed as the first arg to the method
                blueprint_for_this_call=bp_data, # Bake in the blueprint
                log_prefix_for_this_call=func_tool_name # Bake in the log prefix
            )
            # The effective signature FunctionTool sees for `executable_for_tool` should be:
            # (tool_arguments: Optional[Dict[str, Any]] = None)
 
            # ... (augmented_description logic as before, guiding LLM to use the
            #       {"tool_arguments": {...}} wrapper for the 'tool_arguments' parameter) ...
              
            #tool_description = bp_data.get("tool_description", f"Tool {tool_name_orig}")
            tool_description = bp_data.get("tool_description") # Get raw value
            if not isinstance(tool_description, str) or not tool_description.strip(): # If None, not a string, or empty/whitespace
                tool_description = f"MCP tool {tool_name_orig if tool_name_orig else 'UnknownTool'} from server {server_qname if server_qname else 'UnknownServer'}."
                print(f"[McpToolFactory][_create_tools] INFO: Using default description for tool '{func_tool_name}': {tool_description}")
                
            augmented_description = tool_description
            tool_input_schema = bp_data.get("tool_input_schema", {})
            # ... (same augmentation logic as before to explain the structure for 'tool_arguments')
            augmented_description += "\nIMPORTANT: To use this tool, provide a single JSON argument object for the parameter 'tool_arguments'. " # etc.

            print(f"[McpToolFactory][_create_tools] Creating FunctionTool '{func_tool_name}'")
            ft = FunctionTool(
                name=func_tool_name,
                description=augmented_description,
                func=executable_for_tool # Pass the partial object
            )
            created_tools.append(ft)
            print(f"[McpToolFactory][_create_tools] Successfully created FunctionTool: '{func_tool_name}'")
        
        return created_tools

    async def _actual_mcp_executor(self, blueprint: Dict, log_prefix_for_executor: str, **tool_specific_args_from_wrapper):
        """
        The core logic to prepare and send an MCP execution request to the bridge.
        Receives the *actual* tool arguments via **tool_specific_args_from_wrapper.
        """
        captured_server_qname = blueprint['server_qualified_name']
        captured_tool_name = blueprint['tool_name']

        print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] === CORE EXECUTION LOGIC ===")
        print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Tool: {captured_server_qname}/{captured_tool_name}")
        print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Actual args for bridge: {json.dumps(tool_specific_args_from_wrapper)}")
            
        target_profile_id = self.mcp_config.get('smithery_profile_id', "")
        mcp_url_config = {"dynamic": False, "profile": target_profile_id}
        conn_schema = blueprint.get("connection_url_config_schema")
        agent_service_keys = self.mcp_config.get('service_api_keys', {})

        if conn_schema and isinstance(conn_schema, dict):
            schema_props = conn_schema.get("properties", conn_schema)
            if not isinstance(schema_props, dict): schema_props = {}
            for server_expects_key, prop_details in schema_props.items():
                if not isinstance(prop_details, dict): continue
                client_config_key_name = prop_details.get("x-suggested-client-config-key")
                if not client_config_key_name:
                    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', server_expects_key)
                    inferred_key_upper = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).upper()
                    if inferred_key_upper in agent_service_keys: client_config_key_name = inferred_key_upper
                    elif server_expects_key in agent_service_keys: client_config_key_name = server_expects_key
                
                if client_config_key_name and client_config_key_name in agent_service_keys:
                    mcp_url_config[server_expects_key] = agent_service_keys[client_config_key_name]
                    print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Mapped server key '{server_expects_key}' to agent key '{client_config_key_name}'.")
                elif server_expects_key in conn_schema.get("required", []):
                    error_msg = f"Error: Missing required config key '{client_config_key_name or server_expects_key}' for {captured_server_qname}."
                    print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] PREMATURE EXIT: {error_msg}")
                    return error_msg
        
        print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Final mcp_url_config: {json.dumps(mcp_url_config)}")

        exec_content = {
            "action": "execute_mcp_tool",
            "protocol": blueprint.get("protocol_preference", "streamablehttp"),
            "endpoint_id": captured_server_qname,
            "tool_name": captured_tool_name,
            "tool_args": tool_specific_args_from_wrapper,
            "mcp_url_config": mcp_url_config,
            "api_key_for_gateway": self.mcp_config.get('smithery_api_key', "")
        }
        exec_env = Envelope(role="Agent", content=exec_content, agent_name=self.agent_name)
        
        print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Sending exec request to bridge inbox: {self.mcp_config.get('bridge_inbox')}")
        
        try:
            resp_env = await self.mcp_bridge_client_adapter.request_response(
                self.mcp_config['bridge_inbox'], exec_env, timeout=60.0
            )
            if resp_env and isinstance(resp_env, Envelope) and resp_env.content:
                print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Received response from bridge. Status: {resp_env.content.get('status')}")
                if resp_env.content.get("status") == "success":
                    result_data = resp_env.content.get("result", {})
                    if isinstance(result_data, dict) and "content" in result_data:
                        formatted_parts = [item.get("text", json.dumps(item)) for item in result_data.get("content", []) if item]
                        final_str = "\n".join(formatted_parts) if formatted_parts else "Tool executed successfully with no textual output."
                        print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Success. Result: {final_str[:200]}...")
                        return final_str
                    final_str = json.dumps(result_data)
                    print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Success (non-standard result). Result: {final_str[:200]}...")
                    return final_str
                else:
                    error_msg = resp_env.content.get('error', 'Unknown error')
                    details = resp_env.content.get('details', '')
                    full_err = f"Error from bridge: {error_msg}. Details: {details}"
                    print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Bridge error: {full_err}")
                    return full_err
            print(f"[McpToolFactory][Executor][{log_prefix_for_executor}] Error: No valid response from bridge.")
            return "Error: No valid response from bridge."
        except asyncio.TimeoutError:
            return f"Error: Timeout executing tool '{captured_server_qname}/{captured_tool_name}'"
        except Exception as e:
            return f"Error: Exception executing tool '{captured_server_qname}/{captured_tool_name}': {str(e)}"