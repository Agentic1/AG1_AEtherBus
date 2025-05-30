Why it's happening (recap):
BusAdapterV2.request_response calls add_subscription for a unique reply_to channel.
add_subscription (via _subscribe_pattern) creates an asyncio.Task that runs start_bus_subscriptions, which in turn runs discover_and_subscribe.
discover_and_subscribe finds the unique reply_to stream and spawns another asyncio.Task to run subscribe (from bus.py) on that specific stream.
The _collector handler (passed by request_response to add_subscription) receives the correlated reply and sets the future. request_response then returns.
request_response calls remove_subscription. Your updated remove_subscription now cancels the task for start_bus_subscriptions (which runs discover_and_subscribe).
The discover_and_subscribe task, when cancelled, should then cancel the specific subscribe task it spawned for the reply_to channel.
The subscribe task, when cancelled, should exit its XREADGROUP loop.
If all these cancellations and exits happen cleanly and quickly before await redis_client.aclose() in your test script, then there's no error.
If await redis_client.aclose() happens while any of those tasks are still in a await redis.some_command() call, they will get ConnectionError.
The "Malformed envelope" part of the error message is likely a secondary effect: The subscribe loop in bus.py might be trying to process one last piece of data or an internal state when the connection drops, leading it to think it has a malformed envelope because the read was incomplete or errored out due to the closed connection. The root cause is the Connection closed by server.
Immediate Plan:
Focus on Bridge Functionality: You've confirmed the bridge's AetherBus services for discovery and execution are working. This is the primary goal for mcp_bridge_gem.py.
Acknowledge the Cleanup Noise: For now, accept that these trailing errors in the test client are due to the bus library's background task cleanup interacting with the test script's Redis client closure. They are not indicative of a failure in the bridge's request processing.
Proceed to Agent-Side Implementation: Start building the muse_agent.py logic:
initialize_dynamic_tools to call the bridge's discovery service (using BusAdapterV2.request_response).
create_mcp_autogen_tool_wrapper where the wrapper calls the bridge's execution service (using BusAdapterV2.request_response).
Integrate with AutoGen.
Later (Bus Library Refinement - Lower Priority for Bridge Development):

--

TODO: Improve AetherBus Dynamic Subscription Task Cleanup
Issue:
After a successful request-response cycle using BusAdapterV2.request_response (e.g., in test_bridge_client.py), the main test script closes its Redis connection. However, background tasks spawned by BusAdapterV2.add_subscription (specifically the discover_and_subscribe task and any subscribe tasks it created for the temporary reply_to channel) may still be attempting Redis operations. This results in redis.exceptions.ConnectionError: Connection closed by server errors being logged from these background tasks as they try to use the already closed connection. Secondary errors like "Malformed envelope" can also appear as a consequence.
Current Status:
The primary request-response flow between the test client and the mcp_bridge_gem.py (for both discovery and execution) is working correctly. The client receives the expected data.
BusAdapterV2.remove_subscription has been updated to cancel the main task associated with the discover_and_subscribe pattern.
discover_and_subscribe and subscribe functions have basic asyncio.CancelledError handling.
Root Cause:
The cancellation and cleanup of the nested asynchronous tasks (main test client -> BusAdapterV2.request_response -> add_subscription -> start_bus_subscriptions -> discover_and_subscribe -> subscribe) are not fully synchronized with the main client's redis_client.aclose() call. Some background tasks might still be in a await redis.some_command() state when the connection is pulled from under them.
Goal:
Ensure that when BusAdapterV2.remove_subscription is called for a temporary channel (like an RPC reply channel), all associated background Redis-listening tasks are cleanly and fully terminated before the calling context (e.g., the test script) might close the shared Redis client.
Potential Action Items for AetherBus Library (BusAdapterV2, agent_bus_minimal.py, bus.py):
Robustify Task Cancellation Propagation:
Verify that BusAdapterV2.remove_subscription's await task_to_cancel (where task_to_cancel runs start_bus_subscriptions) effectively waits for the entire chain of spawned tasks (discover_and_subscribe and its child subscribe tasks) to acknowledge cancellation and exit their Redis-interacting loops.
Ensure asyncio.CancelledError is caught and handled gracefully at all levels of these nested loops, preventing further Redis operations after cancellation is initiated.
Review discover_and_subscribe's Management of spawned_subscribe_tasks:
Ensure that when discover_and_subscribe itself is cancelled, it reliably cancels and awaits all subscribe tasks it directly spawned. The current asyncio.gather in its CancelledError handler is a good step.
Consider a More Direct Listener for BusAdapterV2.request_response:
Evaluate if BusAdapterV2.request_response should use a self-contained, temporary listener loop (similar to the bus_rpc_call_v2 sketch that used direct redis.xread) instead of relying on the general-purpose add_subscription / remove_subscription which is designed for more persistent, pattern-based subscriptions. This would simplify cleanup for RPC-style calls.
Refine finally Blocks: Ensure finally blocks in discover_and_subscribe and subscribe do not attempt Redis operations if the exit is due to a cancellation that's part of a Redis client shutdown.
Impact:
While not breaking the core functionality of the bridge's services (as the primary replies are received correctly), resolving this will lead to cleaner shutdown in test environments and more robust behavior in long-running, complex multi-agent systems using the AetherBus library.
This summary should capture the essence of the cleanup issue. For now, we can proceed with agent-side development, as the bridge's core request handling is proven.

--
What to Do Next: Clean Up Bus Code or Sort Servers Out?
Recommendation: Sort Servers Out First (Supervisor)
Get Services into Supervisor:
Why: Stability is paramount. Before you dive deep into code refactoring, ensure your core services (aetherdeck_relay_handler.py, a2a_edge_handler.py, tg_edge_handler.py, heartbeat_service_agent.py, ans_search_service.py if you use it, the external BraveSearchA2A agent, MCP bridge if separate) are reliably running under a process manager like supervisorctl.
Benefits:
They auto-restart on crashes.
Consistent startup/shutdown.
Centralized logging (configure supervisor to manage their logs).
You can confidently leave them running while you develop and test other parts.
This will make your life much easier for all subsequent steps.
Then, Concurrently or Sequentially:
Clean Up and Review AetherBus Code (Connectors & Core):
Why: Now that the core flows are working, solidifying the bus logic is key for long-term maintainability and extensibility.
Actions:
RPC (rpc.py): Review bus_rpc_call and bus_rpc_envelope. Ensure they are robust and cover common error cases well. Add more detailed logging if needed. The current versions are much better.
StreamKeyBuilder (keys.py): It's already good, but give it a once-over to ensure all stream patterns are logical and consistently used by all edge handlers and agents. Document these patterns clearly.
Envelope (envelope.py): Ensure all necessary fields are there and well-understood.
BusAdapterV2 (bus_adapterV2.py): This seems to be working well.
Edge Handlers (aetherdeck_relay_handler.py, a2a_edge_handler.py, tg_edge_handler.py):
Standardize their main loop, registration handling, and how they publish to agent inboxes and listen on their specific edge_response streams. They should follow a very similar pattern.
Ensure consistent error handling and logging.
Benefit: Reduces bugs, makes the system easier to understand, and simplifies adding new edge handlers or agents.
Address AI Misinterpretation (LLM Prompt Engineering - Ongoing):
Why: This is crucial for usability.
Actions: As you test, continually refine the system messages and tool descriptions for MuseAgentIntelligent and any other LLM-powered agents (like HeartBeatServiceAgent if it still uses an LLM to decide to use its tool).
Be extremely explicit about JSON formats for tool arguments.
Provide clear examples.
Test edge cases.
Benefit: Makes the AI more reliable and useful.
Then, move to the "Monetization/Funding Checklist" priorities we discussed previously:
Dynamic UI Directives from MuseAgent.
Robust MCP Tool use and presentation.
ANS integration.
You've built a powerful and flexible platform. Taking the time now to stabilize the infrastructure (supervisor) and then solidify the core bus logic will pay huge dividends. Amazing work!
