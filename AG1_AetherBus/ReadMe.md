AG1_AetherBus: The Agentic Communication Backbone
🚀 Overview
The AG1_AetherBus is a high-performance, real-time messaging bus built on Redis Streams. It serves as the central nervous system for the AG1 agentic ecosystem, enabling seamless, decoupled, and scalable communication between various agents, services, and external edge handlers (like Telegram, AetherDeck, and A2A agents).
Its core philosophy is to provide a standardized, robust, and observable communication layer, allowing agents to focus on their intelligence rather than complex networking.
✨ Key Features
Redis Streams Powered: Leverages Redis Streams for message persistence, consumer groups, and real-time delivery.
Standardized Messaging (Envelope): All communication is encapsulated in a well-defined Envelope schema.
Consistent Naming (StreamKeyBuilder): Enforces a structured key system for all Redis streams, ensuring clarity and preventing conflicts.
Flexible Communication Patterns: Supports Pub/Sub, Request-Response (RPC), and Streaming RPC.
Edge Handler Integration: Provides patterns for connecting external platforms (Telegram, WebSockets, A2A) to the internal bus.
Scalability: Built-in support for Redis Consumer Groups allows horizontal scaling of agents and services.
Resilience: Messages are persisted, and consumer groups ensure messages are processed even if consumers go offline temporarily.
🏗️ Architecture & Core Concepts
The AetherBus operates on a publish/subscribe model, where components interact by sending and receiving Envelope messages on named Redis Streams.


+-------------------+     +------------------+     +-------------------+
|    Edge Handlers  |     |                  |     |     Agents &      |
|  (Telegram, AetherDeck, |<--->|   AEtherBus      |<--->|     Services      |
|  A2A, MCP Bridge) |     |   (Redis Streams) |     |  (Muse, HeartBeat, |
+-------------------+     |                   |     |  ANS Search)      |
         ^                 +------------------+     +-------------------+
         |                           ^
         v                           |
+-------------------+     +------------------+
|  External Systems |     |  Monitoring &    |
|  (Users, APIs)    |     |  Management      |
+-------------------+     +------------------+
Use code with caution.
1. Redis Streams
Persistence: Messages are stored in Redis, ensuring no data loss if consumers are offline.
Consumer Groups: Allow multiple instances of a service/agent to consume messages from a single stream, distributing the load and enabling horizontal scaling.
Message IDs: Each message has a unique, time-ordered ID, enabling reliable message tracking.
2. The Envelope (envelope.py)
The universal message format for all communication on the bus. It's a dataclass that gets serialized to JSON.
┌────────────────────────────────────────────────────────────────────┐
      │                           Envelope Schema                         │
      ├───────────────┬───────────────┬───────────────┬───────────────────┤
      │ envelope_id   │ timestamp     │ role          │ agent_name        │
      │ (UUID/str)    │ (ISO-8601)    │ (str)         │ (str)             │
      ├───────────────┼───────────────┼───────────────┼───────────────────┤
      │ envelope_type │ user_id       │ session_code  │ correlation_id    │
      │ (str)         │ (str)         │ (str, optional)│ (UUID/str, optional)│
      ├───────────────┴───────────────┴───────────────┴───────────────────┤
      │ content         (dict)                                           │
      ├───────────────────────────────────────────────────────────────────┤
      │ reply_to (str, optional) │ meta (dict, optional)                 │
      └───────────────────────────────────────────────────────────────────┘
Use code with caution.
envelope_id: Unique ID for this specific message.
timestamp: UTC timestamp of creation.
role: Defines the sender's role (e.g., "user", "agent", "system", "bridge_service", "user_interface_event").
agent_name: The unique identifier of the sending agent/service.
envelope_type: Categorizes the message's purpose (e.g., "message", "register", "a2a_request", "mcp_discovery_result", "error").
user_id: The ID of the end-user associated with the conversation/request.
session_code: Optional identifier for a specific conversation session.
correlation_id: Crucial for RPC patterns, linking requests to their responses.
content: The actual payload of the message. Its structure varies based on envelope_type. (Future: Enforced by Pydantic schemas).
reply_to: The Redis stream where the receiver should publish its response for this specific Envelope.
meta: Optional dictionary for additional, non-standard metadata.
3. Stream Naming Conventions (keys.py)
The StreamKeyBuilder class ensures all Redis stream names follow a consistent, namespaced pattern (AG1:{entity_type}:{entity_id}:{suffix}). Always use StreamKeyBuilder to generate stream keys.
from AG1_AetherBus.keys import StreamKeyBuilder

keys = StreamKeyBuilder(namespace="AG1")

# Agent Inboxes/Outboxes
muse_inbox = keys.agent_inbox("Muse1")         # AG1:agent:Muse1:inbox
heartbeat_outbox = keys.agent_outbox("HeartBeat") # AG1:agent:HeartBeat:outbox

# Edge Service Registration/Response
tg_register = keys.edge_register("tg")         # AG1:edge:tg:register
aetherdeck_response = keys.edge_response("aetherdeck", "user_abc") # AG1:edge:aetherdeck:user_abc:response

# A2A Specific
a2a_register = keys.a2a_register()             # AG1:a2a:register
a2a_response = keys.a2a_response("A2AProxy", "req_xyz") # AG1:a2a:response:A2AProxy:req_xyz

# MCP Bridge

mcp_inbox = keys.mcp_bridge_inbox()            # AG1:edge:mcp:main:inbox
Use code with caution.
Python
🧩 Core Components & Their Functions
1. bus.py: The Core Pub/Sub Engine
Provides the fundamental Redis Stream interaction logic.
build_redis_url(): Constructs the Redis connection URL from environment variables.
ensure_group(redis, channel, group): Ensures a Redis Consumer Group exists for a given stream, creating it if necessary.
publish_envelope(redis, channel, env): Publishes an Envelope object to the specified Redis stream.
Serializes the Envelope to JSON and stores it under the data field in the Redis Stream entry.
Enforces a STREAM_MAXLEN and ENVELOPE_SIZE_LIMIT.
subscribe(redis, channel, callback, group, consumer, ...): The primary subscription function.
Subscribes to a Redis Stream using a Consumer Group.
Reads messages, deserializes them into Envelope objects.
Passes the Envelope to the provided callback function.
Handles message acknowledgment (xack), retries, and basic error logging.
subscribe_simple(redis, stream, callback, ...): A simpler subscriber without consumer groups (use subscribe for most agent scenarios).
2. rpc.py: Request-Response & Streaming RPC Patterns
Provides higher-level functions for synchronous-like communication over the asynchronous bus.
bus_rpc_call(redis, target_stream, request_env, timeout):
Purpose: Performs a single request-response call where the caller needs the raw JSON string payload of the response.
Mechanism: Publishes request_env to target_stream, then actively xreads (polls) request_env.reply_to for a single message within a timeout.
Returns: Optional[str] (the raw JSON string payload of the response, or None on timeout/malformed response).
Usage: Use when the responding service sends a simple JSON string that the caller wants to parse directly, or when the response is not a full Envelope.
bus_rpc_envelope(redis, target_inbox, request_envelope, timeout):
Purpose: The primary RPC function for inter-agent communication. Performs a single request-response call where the caller expects a full Envelope object as a response.
Mechanism: Internally calls bus_rpc_call to get the raw JSON string, then deserializes that string into an Envelope object.
Returns: Optional[Envelope] (the deserialized Envelope object, or None on timeout/malformed response).
Usage: Most common for agent-to-agent RPC, where both sides understand the Envelope contract.
bus_rpc_stream(redis, target_stream, request_env, timeout, max_responses):
Purpose: Initiates a streaming RPC call, yielding multiple Envelope updates over time.
Mechanism: Publishes request_env, then continuously xreads request_env.reply_to, yielding each received Envelope until timeout or max_responses.
Returns: AsyncIterator[Envelope].
Usage: For long-running tasks that provide incremental updates (e.g., progress reports, SSE-like streams).
3. bus_adapterV2.py: Agent-Friendly Bus Interface
Provides a convenient, object-oriented wrapper around the low-level bus functions for agents and services.
BusAdapterV2(agent_id, core_handler, redis_client, patterns, group): Initializes the adapter.
start(): Starts subscriptions for all configured patterns.
add_subscription(pattern, handler): Dynamically adds a new subscription.
remove_subscription(pattern): Cleans up and cancels a subscription task.
publish(stream, env): Publishes an Envelope.
request_response(stream, req_env, timeout): Performs a single request-response using bus_rpc_envelope (internally).
wait_for_next_message(...): Utility for one-off message collection.
4. agent_bus_minimal.py: Agent Bus Utilities
Contains helper functions for common agent bus interactions, particularly registration.
register_with_tg_handler(config, redis): Sends a registration Envelope to the Telegram edge handler.
register_with_aetherdeck_handler(config, redis, user_id_pattern): Sends a registration Envelope to the AetherDeck edge handler.
start_bus_subscriptions(redis, patterns, group, handler): Spawns background tasks for discover_and_subscribe (if patterns are dynamic) or direct subscribe calls.
🤝 Interaction Patterns & Message Flow
1. Basic Pub/Sub (Fire-and-Forget)
An agent publishes an Envelope to a stream, and any subscribed consumer group members receive it. No immediate response is expected by the publisher.

+-----------+       publish_envelope()       +-----------------+
| Publisher |------------------------------->| Redis Stream    |
| (Agent/   |                                +-----------------+
| Service)  |                                        |
+-----------+                                        | subscribe()
                                                     V
                                             +-----------------+
                                             | Consumer Group  |
                                             | (Agent/Service) |
                                             +-----------------+
Use code with caution.
2. RPC (Request-Response)
A caller sends a request and waits for a single, specific response.
+-----------+                                +-----------------+
| Caller    |  1. bus_rpc_envelope_call()    | Redis Stream    |
| (Agent/   |  (Publishes request_env to     | (Target Inbox)  |
| Service)  |   target_stream)               +-----------------+
+-----------+                                        |
      ^                                                | subscribe()
      |                                                V
      | 4. Returns response_env                      +-----------------+
      |                                              | Responder       |
      |                                              | (Agent/Service) |
      +----------------------------------------------+-----------------+
        3. publish_envelope() (response_env to reply_to)
        (Responder publishes response to request_env.reply_to)


Detailed Flow:
Caller: Creates request_env with reply_to (a unique stream for this response) and correlation_id. Calls await bus_rpc_envelope_call(redis, target_inbox, request_env).
bus_rpc_envelope_call: Publishes request_env to target_inbox. Then, it xreads (polls) request_env.reply_to for a message matching correlation_id within a timeout.
Responder: Subscribes to target_inbox. Receives request_env. Processes it. Creates response_env (copying correlation_id and user_id from request_env). Publishes response_env to request_env.reply_to.
bus_rpc_envelope_call: Receives response_env, deserializes it, and returns it to the Caller.
3. Edge Handler Registration & Routing (e.g., AetherDeck)
Edge handlers act as bridges, translating external channel protocols to bus Envelopes and vice-versa. Agents register with them.
+-----------------+      WebSocket      +---------------------+      Redis (AetherBus)      +-----------------+
| AetherDeck      |-------------------->| aetherdeck_relay_handler.py |----------------------------->| MuseAgent       |
| (Frontend UI)   | (1. UI Event JSON)  | (AetherDeck Handler)| (2. Envelope_AD_to_Agent)   | (Core Logic)    |
+-----------------+                     +---------------------+                             +-----------------+
        ^                                       ^     ^                                             ^
        |                                       |     |                                             |
        | (7. UI Directive JSON)                |     | (3. Agent Registration)                     | (6. Agent Reply/UI Directive)
        |                                       |     |                                             |
        +---------------------------------------+     +---------------------------------------------+
                                                      |                                             |
                                                      | (4. Envelope_Agent_to_AD_Directive)         |
                                                      |                                             |
                                                      +---------------------------------------------+

Detailed Flow:

1.  **AetherDeck Frontend -> `aetherdeck_relay_handler.py`:**
    *   **Channel:** WebSocket (`wss://vroll.evasworld.net/4002/`)
    *   **Content:** Raw AetherDeck UI Event (JSON: `{"event_type": "user_chat_input", "text": "Hello"}`)
    *   **`aetherdeck_relay_handler.py` Action:** Receives WS message, parses JSON, extracts `user_id`, `session_code`.

2.  **`aetherdeck_relay_handler.py` -> MuseAgent (or other registered agent):**
    *   **Stream:** `AG1:agent:Muse1:inbox` (or `AG1:agent:<RegisteredAgentName>:inbox`)
    *   **Content:** `Envelope`
        *   `role`: "user"
        *   `user_id`: (from AetherDeck WS)
        *   `session_code`: (from AetherDeck WS, if any)
        *   `reply_to`: `AG1:edge:aetherdeck:<user_id>:response` (This is the unique stream for UI directives back to *this specific AetherDeck client*)
        *   `content`: `{"text": "Hello", "source_channel": "aetherdeck"}` (Normalized text from AetherDeck event)
        *   `envelope_type`: "message"
    *   **`aetherdeck_relay_handler.py` Action:** Looks up `user_id` in `aetherdeck_registered_agents` (populated by step 3), publishes to the found agent's inbox.

3.  **MuseAgent -> `aetherdeck_relay_handler.py` (Registration):**
    *   **Stream:** `AG1:edge:aetherdeck:register`
    *   **Content:** `Envelope`
        *   `role`: "agent"
        *   `envelope_type`: "register"
        *   `agent_name`: "Muse1"
        *   `content`: `{"channel_type": "aetherdeck", "aetherdeck_user_id_pattern": "all", "agent_inbox_stream": "AG1:agent:Muse1:inbox"}`
    *   **`aetherdeck_relay_handler.py` Action:** Subscribes to this stream, updates its `aetherdeck_registered_agents` dictionary.

4.  **MuseAgent -> `aetherdeck_relay_handler.py` (UI Directive/Final Response):**
    *   **Stream:** `AG1:edge:aetherdeck:<user_id>:response` (This is the `reply_to` from step 2)
    *   **Content:** `Envelope`
        *   `role`: "agent"
        *   `user_id`: (original AetherDeck user_id)
        *   `reply_to`: (original AetherDeck reply_to, not used by relay here)
        *   `content`: **AetherDeck UI Directive JSON (Python dict)** (e.g., `{"directive_type": "CREATE_WINDOW", ...}` or `{"text": "Agent's plain text reply"}`)
        *   `envelope_type`: "message"
    *   **`aetherdeck_relay_handler.py` Action:** Subscribes to this stream (per connected AetherDeck client), receives `Envelope`, extracts `content`, and sends it over the WebSocket.

5.  **`aetherdeck_relay_handler.py` -> AetherDeck Frontend:**
    *   **Channel:** WebSocket
    *   **Content:** Raw AetherDeck UI Directive (JSON string, converted from `Envelope.content` by `send_json`)

## 🛠️ Best Practices & Guidelines

1.  **Always Use `StreamKeyBuilder`:** Never hardcode stream names. This ensures consistency and prevents routing errors.
2.  **`Envelope` is the Contract:** All messages must be `Envelope` objects. Ensure they conform to the schema.
3.  **Validate `Envelope.content`:** For robust systems, define Pydantic schemas for the `content` field for each `envelope_type`. Validate `content` on both send and receive.
4.  **Handle `Optional` Returns:** `bus_rpc_call` and `bus_rpc_envelope` return `Optional` types. Always check for `None`.
5.  **Acknowledge Messages (`xack`):** When using `subscribe` with consumer groups, always `xack` messages after successful processing (or after deciding to dead-letter them) to prevent re-processing.
6.  **Graceful Shutdown:** Implement `asyncio.CancelledError` handling in long-running tasks (like `subscribe` loops) to ensure clean shutdowns.
7.  **Logging:** Use consistent logging prefixes (e.g., `[AGENT_NAME]`) and provide sufficient detail for debugging message flow.
8.  **Error Handling:** Implement `try-except` blocks for network calls, JSON parsing, and tool executions. Publish `envelope_type="error"` messages when appropriate.
9.  **Security (Future):** For production, implement Redis ACLs to restrict client permissions and robust authentication/authorization at edge handlers.
10. **Documentation:** Keep this `README.md` and `STREAM_KEYS.md` updated. Document the expected `content` schemas for each `envelope_type`.

## 🚀 Quick Start & Usage

### Prerequisites

*   Redis server (version 6.0+ recommended for ACLs and full Stream features)
*   Python 3.10+
*   `pip install -r requirements.txt` (including `redis`, `aiohttp`, `aiohttp-cors`, `python-dotenv`, `aiogram`, `httpx`, `beautifulsoup4`, `pyyaml`, `mcp` if using all components)

### Example: Publishing a Message

```python
import asyncio
from redis.asyncio import Redis
from AG1_AetherBus.bus import publish_envelope, build_redis_url
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

async def main():
    redis = await Redis.from_url(build_redis_url())
    keys = StreamKeyBuilder()

    user_id = "test_user_123"
    agent_name = "cli_publisher"
    target_agent_inbox = keys.agent_inbox("Muse1") # Assuming Muse1 is listening

    # Create an Envelope
    message_env = Envelope(
        role="user",
        user_id=user_id,
        agent_name=agent_name,
        envelope_type="message",
        content={"text": "Hello Muse, how are you today?", "source_channel": "cli"},
        reply_to=keys.user_inbox(user_id), # Where Muse should reply if it's a direct user message
        correlation_id=str(uuid.uuid4())
    )

    # Publish the Envelope
    await publish_envelope(redis, target_agent_inbox, message_env)
    print(f"Published message to {target_agent_inbox}")

    await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
Use code with caution.
Example: Subscribing to a Stream
import asyncio
from redis.asyncio import Redis
from AG1_AetherBus.bus import subscribe, build_redis_url
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

async def my_callback_handler(env: Envelope):
    """Callback function to process received Envelopes."""
    print(f"\n[MySubscriber] Received Envelope:")
    print(f"  ID: {env.envelope_id}")
    print(f"  From: {env.agent_name} (Role: {env.role})")
    print(f"  Type: {env.envelope_type}")
    print(f"  Content: {env.content}")
    print(f"  Reply To: {env.reply_to}")

async def main():
    redis = await Redis.from_url(build_redis_url())
    keys = StreamKeyBuilder()

    # Subscribe to Muse1's inbox (assuming Muse1 is running and publishing there)
    # Or subscribe to a specific edge response stream for testing
    channel_to_subscribe = keys.agent_inbox("Muse1") # Or keys.edge_response("aetherdeck", "test_user_id")
    consumer_group = "my_test_group"
    consumer_name = "my_test_consumer"

    print(f"[MySubscriber] Subscribing to {channel_to_subscribe}...")
    await subscribe(
        redis,
        channel_to_subscribe,
        my_callback_handler,
        group=consumer_group,
        consumer=consumer_name
    )
    # This will run indefinitely

if __name__ == "__main__":
    asyncio.run(main())
Use code with caution.
Python
Example: Making an RPC Call (using bus_rpc_envelope)
import asyncio
from redis.asyncio import Redis
from AG1_AetherBus.bus import build_redis_url
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.rpc import bus_rpc_envelope # Import the RPC function

async def main():
    redis = await Redis.from_url(build_redis_url())
    keys = StreamKeyBuilder()

    caller_agent_name = "test_rpc_caller"
    target_agent_name = "HeartBeat" # Assuming HeartBeatServiceAgent is running
    target_inbox = keys.agent_inbox(target_agent_name)

    # Create the request Envelope
    request_env = Envelope(
        role="user",
        user_id="rpc_test_user",
        agent_name=caller_agent_name,
        envelope_type="message",
        content={"text": "What is the network status?"},
        # reply_to will be generated by bus_rpc_envelope if not set, but good practice to set
        reply_to=keys.agent_rpc_reply(caller_agent_name, str(uuid.uuid4())),
        correlation_id=str(uuid.uuid4())
    )

    print(f"[RPC_Caller] Sending RPC request to {target_agent_name}...")
    response_envelope = await bus_rpc_envelope(
        redis,
        target_inbox,
        request_env,
        timeout=10.0 # Wait up to 10 seconds for a response
    )

    if response_envelope:
        print(f"\n[RPC_Caller] Received RPC Response:")
        print(f"  From: {response_envelope.agent_name}")
        print(f"  Content: {response_envelope.content}")
        print(f"  Correlation ID: {response_envelope.correlation_id}")
    else:
        print(f"\n[RPC_Caller] No valid RPC response received from {target_agent_name} within timeout.")

    await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())