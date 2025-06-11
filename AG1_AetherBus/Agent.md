# Agent Codex: AG1_AetherBus Library

**Version:** (e.g., 0.6.0 - Increment after cleanup)

**Agent Type:** Core Infrastructure Library / Message Bus SDK

**Primary Goal:**
To provide a reliable, asynchronous, Redis Streams-based message bus for inter-agent communication within the AG1 ecosystem. It defines core message structures (Envelope), publishing/subscribing mechanisms, and RPC-like patterns.

**Key Components & Modules:**

1.  **`envelope.py` - The `Envelope` Class:**
    *   **Purpose**: Defines the standardized message wrapper for all AetherBus communications.
    *   **Core Fields**: `role`, `content`, `agent_name`, `user_id`, `session_code`, `reply_to`, `correlation_id`, `envelope_type`, `meta`, `timestamp`, `envelope_id`.
    *   **Functionality**: Serialization (`to_dict`), deserialization (`from_dict`), unique ID generation.

2.  **`keys.py` - `StreamKeyBuilder` Class:**
    *   **Purpose**: Provides a centralized and consistent way to generate Redis stream names and keys for various AetherBus entities (agent inboxes, edge responses, temporary RPC replies, registration channels, etc.).
    *   **Functionality**: Methods like `agent_inbox(name)`, `edge_response(edge_name, user_id)`, `edge_register(edge_name)`, `temp_response(unique_id_part)`.

3.  **`bus.py` - Core Bus Operations:**
    *   **`build_redis_url(...)`**: Constructs the Redis connection URL from configuration (environment variables or direct parameters).
    *   **`publish_envelope(redis_client, stream_name, envelope)`**: Serializes an `Envelope` and publishes it to a specified Redis Stream using `XADD`.
    *   **`subscribe_simple(redis_client, stream_name, callback, group_name, consumer_name, mkstream, block_ms, is_temp_stream)`**: A simplified, robust function for an agent to subscribe to a single stream using a consumer group, process messages via a callback, and handle acknowledgments.
    *   **`subscribe(redis_client, stream_patterns_or_names, callback, group, consumer, mkstream, batch_size, block_ms)`**: (If this is a more general or older version) A function for subscribing to one or more streams/patterns. *Focus for cleanup might be to ensure `subscribe_simple` is the primary, well-tested method for single stream listening by agents, or to make `subscribe` equally robust.*

4.  **`rpc.py` - Request-Response Pattern Implementation:**
    *   **`bus_rpc_call(redis_client, target_stream, request_envelope, timeout)`**:
        *   Sends `request_envelope` to `target_stream`.
        *   **Crucially, it manages a temporary, unique reply stream name which it sets in `request_envelope.reply_to` before sending.**
        *   Listens on this temporary reply stream for a single response envelope (as a JSON string).
        *   Handles timeouts.
        *   Returns the JSON string of the response envelope or `None`.
    *   **`bus_rpc_envelope(redis_client, target_inbox, request_envelope, timeout)`**:
        *   A wrapper around `bus_rpc_call`.
        *   Ensures `request_envelope.reply_to` is appropriately set (using a fallback if necessary) before calling `bus_rpc_call`.
        *   Takes the JSON string response from `bus_rpc_call` and deserializes it into an `Envelope` object (or an error dictionary).

5.  **`bus_adapterV2.py` - `BusAdapterV2` Class:**
    *   **Purpose**: An abstraction layer for agents to easily manage subscriptions to their inboxes and other streams.
    *   **Functionality**:
        *   Initializes with an `agent_id`, `core_handler` (like `agent.handle_bus_envelope`), `redis_client`, and `patterns` to listen on.
        *   Uses `subscribe_simple` (or `subscribe`) internally to manage the Redis Stream subscriptions and consumer groups.
        *   Provides `start()` and `stop()` methods for managing the lifecycle of the listeners.
        *   Handles dispatching received messages to the `core_handler`.
        *   May include methods like `add_subscription()` for dynamic subscriptions.

**Dependencies:**
*   `redis` (asyncio version: `redis.asyncio`)
*   Python 3.x (specify version, e.g., 3.9+)

**Key Design Principles for Robustness:**
*   Clear error handling and logging in all bus operations.
*   Resilience to Redis connection issues (e.g., retry mechanisms where appropriate).
*   Proper message acknowledgment (`XACK`) in subscription loops to prevent reprocessing of faulty messages.
*   Consistent envelope serialization and deserialization.
*   Well-defined and unique stream naming via `StreamKeyBuilder`.
*   Non-blocking operations suitable for an `asyncio` environment.

**Interaction with Other Agents:**
This library is the fundamental communication layer. All AetherBus agents (Muse2, HeartBeat, Echo, EthAgent, Gatekeeper, uFetch_ASIOne_Client_Agent, ufetch_asi_edge_handler, Relay, etc.) will use these components to send and receive messages.