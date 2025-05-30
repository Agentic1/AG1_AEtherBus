+-----------------+      AetherBus (Redis Streams)      +----------------------+      MCP Ecosystem
| muse_agent.py   |------------------------------------->| mcp_bridge_gem.py    |--------------------->+
| (AutoGen Core & |                                      | (MCP Gateway)        |<---------------------+
|  Setup Logic)   |<-------------------------------------|                      |                      |
+-----------------+                                      +----------------------+                      |
        |                                                                                              |
        | 1. Muse Setup: "Need to discover tools"                                                      |
        |    (e.g., for "twitter" or server "@foo/bar")                                                |
        |                                                                                              |
        | 2. Muse Setup -> AetherBus:                                                                  |
        |    ENVELOPE (to Bridge Inbox)                                                                |
        |    Content: {                                                                                |
        |      "action": "discover_mcp_tools",                                                         |
        |      "discovery_target": {"type": "search", "query": "twitter"},  <------+                   |
        |      "mcp_session_config": {...}, "api_key": "..."                     |                   |
        |      "reply_to": "muse_discovery_reply_channel"                        |                   |
        |    }                                                                    |                   |
        |                                                                         |                   |
        +-------------------------------------------------------------------------+ 3. Bridge receives Envelope
                                                                                  |
                                                                                  | 4. Bridge (internally):
                                                                                  |    Uses python-sdk:
                                                                                  |    async with mcp.connect(target_discovery_svc_url, config) as client:
                                                                                  |        manifest_json = await client.list_tools() OR
                                                                                  |                        await client.call_tool("search_tool", args)
                                                                                  |
                                                                                  | 5. Bridge -> AetherBus:
                                                                                  |    ENVELOPE (to "muse_discovery_reply_channel")
                                                                                  |    Content: {
                                                                                  |      "status": "success",
                                                                                  |      "manifest": manifest_json  <-----------------------------------+
                                                                                  |    }                                                                    |
                                                                                  |                                                                         |
        +-------------------------------------------------------------------------+ 6. Muse Setup receives manifest                                         |
        |                                                                         |                                                                         |
        | 7. Muse Setup:                                                          |                                                                         |
        |    For each tool in manifest:                                           |                                                                         |
        |      wrapper_func, schema = create_mcp_autogen_tool_wrapper(tool_info)  |                                                                         |
        |    Register these with MuseAgentIntelligent (AutoGen)                   |                                                                         |
        |                                                                         |                                                                         |
        |--------------------------- Run AutoGen Chat ----------------------------|                                                                         |
        |                                                                         |                                                                         |
        | 8. MuseAgentIntelligent (LLM): Decides to use "discovered_tool_X"       |                                                                         |
        |    Calls wrapper_func_for_tool_X(args_from_llm)                         |                                                                         |
        |                                                                         |                                                                         |
        | 9. wrapper_func_for_tool_X -> AetherBus:                                |                                                                         |
        |    ENVELOPE (to Bridge Inbox)                                           |                                                                         |
        |    Content: {                                                           |                                                                         |
        |      "action": "execute_mcp_tool",  <-----------------------------------+                                                                         |
        |      "endpoint": "mcp_server_id_from_manifest",                         |                                                                         |
        |      "tool": "tool_X_name_from_manifest",                               |                                                                         |
        |      "args": args_from_llm,                                             |                                                                         |
        |      "mcp_session_config": {...}, "api_key": "..."                      |                                                                         |
        |      "reply_to": "muse_execution_reply_channel_for_this_call"           |                                                                         |
        |    }                                                                    |                                                                         |
        |                                                                         |                                                                         |
        +-------------------------------------------------------------------------+ 10. Bridge receives Envelope
                                                                                  |
                                                                                  | 11. Bridge:
                                                                                  |     Selects protocol (WS, SSE, HTTP POST) based on endpoint/action
                                                                                  |     Uses make_ws_url() etc.
                                                                                  |     Calls call_mcp(), call_mcp_via_sse(), or call_tool_via_rest_post()
                                                                                  |     (This part is mostly what you have, but needs protocol selection)
                                                                                  |
                                                                                  +----------------------> MCP Tool Server
                                                                                  |                      (e.g., Smithery, local)
                                                                                  |
                                                                                  |     Receives result from MCP Tool Server <--------------------------+
                                                                                  |
                                                                                  | 12. Bridge -> AetherBus:
                                                                                  |     ENVELOPE (to "muse_execution_reply_channel_for_this_call")
                                                                                  |     Content: {
                                                                                  |       "status": "success",
                                                                                  |       "result": mcp_tool_result_json  <----------------------------+
                                                                                  |     }                                                                |
                                                                                  |                                                                      |
        +-------------------------------------------------------------------------+ 13. wrapper_func_for_tool_X receives result, returns to AutoGen        |
        |                                                                                                                                                |
        | 14. AutoGen flow continues (e.g., Coordinator summarizes, sends to User via AetherBus)                                                         |
        +------------------------------------------------------------------------------------------------------------------------------------------------+



Action Plan: Steps
Phase 1: Enhance mcp_bridge_gem.py (Bridge Side)
Step 1.1: Define AetherBus "Action" Payloads
Action: In process_envelope, check for content.action.
If content.action == "discover_mcp_tools": Handle discovery.
If content.action == "execute_mcp_tool" (or if action is missing, assume execution for backward compatibility initially): Handle execution.
Else: Error (unknown action).
Goal: process_envelope can route to different internal handlers.
Step 1.2: Implement Discovery Handler in Bridge (handle_discovery_request)
Action: Create a new async function async def handle_discovery_request(env_content, redis, reply_to_channel):
Action: Inside this function:
Parse env_content.discovery_target (e.g., type, query, toolbox_endpoint, server_mcp_endpoint).
Parse env_content.mcp_session_config and env_content.api_key.
Use modelcontextprotocol.python-sdk:
Construct the target MCP URL for discovery (e.g., Smithery Toolbox URL or direct server MCP URL). You'll need to manage how API keys and session configs are passed to mcp.connect() or formatted into the URL based on SDK requirements.
async with mcp.connect(discovery_url, config=mcp_session_config_for_sdk) as client:
If discovery_target.type == "search":
manifest_data = await client.call_tool(SEARCH_TOOL_NAME, {"query": discovery_target.query}) (You'll need to know SEARCH_TOOL_NAME for Smithery Toolbox, e.g., "search_servers").
If discovery_target.type == "direct_server_manifest":
manifest_data = await client.list_tools() (or mcp_get_manifest).
Prepare a success/error response Envelope with the manifest_data.
await publish_envelope(redis, reply_to_channel, response_envelope).
Goal: Bridge can receive a discovery request on AetherBus, use the SDK to fetch a manifest, and send it back on AetherBus.
Step 1.3: Implement Protocol Selection in Execution Handler (handle_execution_request)
Action: Refactor the current tool execution logic in process_envelope into async def handle_execution_request(env_content, redis, reply_to_channel):
Action: Inside this function (it will look similar to your current process_envelope try/except block for execution):
Get endpoint, tool, args, mcp_call_config, api_key_for_call from env_content.
Add Protocol Selection Logic:
Based on endpoint_from_env (e.g., if it starts with wss:// or is a known Smithery alias, or if env_content includes a protocol: "websocket" field), choose to call:
await call_mcp(...) (for WebSockets)
await call_mcp_via_sse(...) (for SSE)
await call_tool_via_rest_post(...) (for simple HTTP POST)
The rest (error handling, publishing result) remains similar.
Goal: Bridge can execute MCP tools using different protocols based on the request.
Phase 2: Implement on muse_agent.py Side (Agent Side)
Step 2.1: Create discover_tools_for_agent Function (in muse_agent.py setup)
Action: Write an async function async def discover_tools_for_agent(redis_client, discovery_payload: dict) -> list:.
discovery_payload will be like {"type": "search", "query": "twitter"} or {"type": "direct", "qualified_name": "@foo/bar"}.
This function will:
Construct the full "Discovery Request" Envelope.content (including action: "discover_mcp_tools", target details, API keys, session configs from a global agent config).
Use bus_rpc_call (or a similar publish/subscribe pattern) to send this envelope to the bridge's inbox and await the manifest response envelope.
Parse the manifest from the response.
Return the list of tool definitions from the manifest.
Goal: Agent setup logic can call this function to get tool manifests via the bridge.
Step 2.2: Implement create_mcp_autogen_tool_wrapper
Action: Implement the create_mcp_autogen_tool_wrapper function as discussed previously.
Input: MCP tool details (name, description, inputSchema from manifest), mcp_server_endpoint_id (e.g., qualified name), global configs (bridge inbox, API keys, Smithery profile).
Output: A Python callable (wrapper) and an AutoGen-compatible JSON schema.
Wrapper Logic: The generated wrapper function, when called by AutoGen, will:
Construct an "Execution Request" Envelope.content (with action: "execute_mcp_tool", the specific endpoint, tool, args from AutoGen, API key, session config).
Use bus_rpc_call to send this to the bridge's inbox and await the execution result.
Return the result to AutoGen.
Goal: A function that can generate AutoGen-compatible tools from MCP manifest entries.
Step 2.3: Agent Startup Logic for Dynamic Tool Registration
Action: In muse_agent.py's main_agentbus or a similar setup phase:
Load bootstrap configs (API keys, Smithery profile, bridge inbox, initial discovery queries/targets).
Call discover_tools_for_agent for each initial query/target.
Iterate through the combined list of discovered MCP tools.
For each MCP tool, call create_mcp_autogen_tool_wrapper.
Register the returned callables and schemas with MuseAgentIntelligent (or the agent designated to use these tools).
Goal: MuseAgentIntelligent starts up with dynamically discovered and registered MCP tools.
Step 2.4: Test with AutoGen
Action: Run muse_agent.py.
Action: Send a message via Telegram (or however you interact with it) that should trigger one of the newly registered dynamic MCP tools.
Goal: Observe the full flow: discovery (at startup), registration, LLM choosing the tool, wrapper calling the bridge, bridge executing the MCP tool, result returning through the layers.
Initial Focus:
Bridge: Get handle_discovery_request working for one method (e.g., direct_server_manifest by calling list_tools() on your local PubMed server if it supports it, or on a known public MCP server).
Bridge: Ensure handle_execution_request can correctly choose the protocol for your local PubMed server (likely call_tool_via_rest_post) and a Smithery WebSocket tool (call_mcp).
Agent Side: Implement discover_tools_for_agent to call the bridge's new discovery action.
Agent Side: Implement create_mcp_autogen_tool_wrapper focusing on one tool first.
Agent Side: Wire up the startup logic to discover and register that one tool.
This iterative approach will allow you to build and test each part of this more complex system.