# Agentic1 Redis Messaging Bus

This system uses Redis Streams to handle real-time messaging between agents and users. Each user has their own inbox stream (e.g. `user.<user_id>.inbox`), and agents dynamically discover and subscribe to new user streams.

---

# Quick Integration Test: End-to-End MCP Bridge Demo

To test the full AG1 MCP bridge workflow (publish, process, observe results):

1. **Start the MCP Bridge** (processes requests from the inbox):

   ```bash
   python core_bus/mcp_bridge.py
   ```

2. **Publish Example MCP Requests** (pushes test envelopes to the bridge):

   ```bash
   python tests/demo_publish_mcp_all_examples.py
   ```

3. **View Results in Real Time** (tail the outbox for bridge responses):

   ```bash
   python core_bus/tail.py mcp_bridge.outbox
   ```

- The test script pushes example Envelopes to `mcp_bridge.inbox`.
- The bridge processes these and sends results to `mcp_bridge.outbox`.
- The tail tool displays the results as soon as they arrive.

If you see results in the outbox, the system is working end-to-end!

---

## How It Works

- **Each user** gets a personal inbox: `user.<user_id>.inbox`
- The system scans for new streams matching `user.*.inbox`
- When discovered, it **spawns a dedicated async subscriber**
- Messages are handled by the PersonalAssistant (`pa0.py`) or another agent

---

## 📬 Redis Stream/Mailbox Structure

This project uses a **namespaced, structured key system** for all Redis streams, making it easy to route, discover, and manage messages between users, agents, flows, sessions, and edges.

### Key Naming Conventions

All keys are prefixed with the namespace (default: `AG1`).

| Purpose          | Example Key Format                      | Example Value                  |
|------------------|----------------------------------------|-------------------------------|
| User Inbox       | `AG1:user:{user_id}:inbox`              | `AG1:user:123:inbox`          |
| Agent Inbox      | `AG1:agent:{agent_id}:inbox`            | `AG1:agent:alpha:inbox`       |
| Agent Outbox     | `AG1:agent:{agent_id}:outbox`           | `AG1:agent:alpha:outbox`      |
| Flow Input       | `AG1:flow:{flow_id}:input`              | `AG1:flow:myflow:input`       |
| Flow Output      | `AG1:flow:{flow_id}:output`             | `AG1:flow:myflow:output`      |
| Session Stream   | `AG1:session:{session_code}:stream`     | `AG1:session:xyz123:stream`   |
| Edge Register    | `AG1:edge:{platform}:register`          | `AG1:edge:telegram:register`  |
| LLM Requests     | `AG1:edge:llm:requests`                 | `AG1:edge:llm:requests`       |
| LLM Register     | `AG1:edge:llm:register`                 | `AG1:edge:llm:register`       |
| Mail Register    | `AG1:edge:mail:register`                | `AG1:edge:mail:register`      |
| Nostr Register   | `AG1:edge:nostr:register`               | `AG1:edge:nostr:register`     |

> **Note:** Always use the `StreamKeyBuilder` class to generate these keys in code.

### Example: Creating Keys in Code

```python
from AG1_AetherBus.keys import StreamKeyBuilder

keys = StreamKeyBuilder(namespace="AG1")
user_inbox = keys.user_inbox("123")         # AG1:user:123:inbox
agent_outbox = keys.agent_outbox("alpha")   # AG1:agent:alpha:outbox
flow_input = keys.flow_input("myflow")      # AG1:flow:myflow:input
edge_reg = keys.edge_register("telegram")   # AG1:edge:telegram:register
```

### How Agents and Users Interact

- **Users** send messages to their own inbox (`user.{user_id}.inbox`).
- **Agents** monitor user inboxes, process messages, and reply via their outbox or directly to user inboxes.
- **Flows** represent multi-step processes, with input/output streams.
- **Sessions** can be tracked for conversation or transaction state.
- **Edges** represent integrations with external platforms (e.g., Telegram).

---

## Developer Flow (CLI / SDK)

1. No need to manually announce a user anymore 
2. Just send a message via `publish_envelope(...)` — this:
   - Creates the stream (if not exists)
   - Triggers discovery automatically
   - Delivers the message to the correct inbox

```python
env = Envelope(
    role="user",
    user_id="myuser123",
    reply_to="user.myuser123.inbox",
    content={"text": "hello"},
    agent_name="cli_bridge",
    envelope_type="message"
)

await publish_envelope(redis, env.reply_to, env)
```

That’s it. The system handles discovery, subscriptions, and routing.

---

## Behind the Scenes

- Discovery uses `SCAN` + `asyncio.create_task()` to keep subscriptions non-blocking
- First message in each inbox is always processed (`xreadgroup` uses `'0'` once)
- Dead-letter handling is built-in (after configurable retry count)

---

## 🧩 Key Components

| File             | Purpose                                |
|------------------|----------------------------------------|
| `bus.py`         | Pub/sub engine with discovery          |
| `pa0.py`         | Main entry agent (PersonalAssistant)   |
| `cli_bridge.py`  | CLI interface for user interaction     |
| `envelope.py`    | Message schema + metadata              |

#----------






# core_bus

Message bus SDK for AG1. Defines the Envelope schema and helpers for publish/subscribe on Redis Streams.

## Envelope Schema

All messages on the bus are wrapped in an `Envelope` object, serialized as JSON. This ensures consistency and evolvability across all services.

```
      ┌────────────────────────────────────────────────────────────────────┐
      │                           Envelope Schema                         │
      ├───────────────┬───────────────┬───────────────┬───────────────────┤
      │ id            │ ts            │ channel       │ headers           │
      │ (UUID/ULID)   │ (ISO-8601)    │ (str)         │ {str: str}        │
      ├───────────────┴───────────────┴───────────────┴───────────────────┤
      │ payload         (dict | str)                                     │
      ├───────────────────────────────────────────────────────────────────┤
      │ version (int) │ reply_to (str) │ trace (list[str])               │
      └───────────────────────────────────────────────────────────────────┘
```

| Field      | Type              | Required | Purpose                                      |
|------------|-------------------|----------|----------------------------------------------|
| id         | ULID/UUIDv7/str   | ✔︎        | Globally unique message id (sortable)        |
| ts         | ISO-8601 string   | ✔︎        | Creation timestamp (UTC)                     |
| channel    | str               | ✔︎        | Routing key (e.g., tasks.create)             |
| headers    | dict[str, str]    | ✔︎        | Fast filters & correlation (max 10 keys)     |
| payload    | dict or str       | ✔︎        | Message body (versioned)                     |
| version    | int               | ✔︎        | Schema version of payload                    |
| reply_to   | str (optional)    | —        | Where a consumer should answer               |
| trace      | list[str]         | —        | Hop list for debugging                       |

**Transport encoding:**
- `json.dumps(envelope).encode("utf-8")` → XADD/XREAD (Redis Streams)
- **Size limit:** 128 KB per message (put large blobs in S3/object storage & pass the URI)

## Canonical Channels (examples)

| Channel         | Producer(s)          | Consumer(s)        | Payload v1                          |
|-----------------|---------------------|--------------------|-------------------------------------|
| tasks.create    | Conversation-Agent  | Task-Manager       | { task_id, user_id, agent_hint, args } |
| billing.request | Task-Manager        | Billing-Service    | { task_id, amount, ... }            |
| tasks.result    | Task-Manager        | PA/Orchestrator    | { task_id, status, result }         |

## AG1 Bus Architecture (ASCII)

```
BrainProject3/
└── AG1/
    ├── core_bus/
    │   ├── envelope.py      # Envelope schema (strict contract)
    │   ├── bus.py           # Reliable pub/sub (Redis Streams, consumer groups)
    │   ├── tail.py          # CLI traffic viewer
    │   └── README.md        # Schema, usage, and ASCII docs
    │
    ├── task_manager_service/
    │   └── main.py          # Listens on tasks.create, echoes results
    │
    ├── billing_service/
    │   └── echo.py          # Echoes canned billing responses
    │
    ├── agent_registry_service/
    │   └── main.py          # (Stub for future service wrapper)
    │
    ├── tests/
    │   ├── test_core_bus.py # Unit/integration tests for bus + envelope
    │   └── README.md
    │
    └── README.md            # AG1 overview & migration plan

      ┌──────────────┐         publish/subscribe        ┌──────────────┐
      │ Producer(s)  │ ───────────────────────────────► │ Consumer(s)  │
      └──────────────┘                                  └──────────────┘
             │                                                ▲
             ▼                                                │
     ┌─────────────────────────────┐                 ┌─────────────────────────────┐
     │  core_bus.publish()         │                 │  core_bus.subscribe()       │
     └────────────┬────────────────┘                 └────────────┬────────────────┘
                  │                                              │
                  ▼                                              │
        ┌─────────────────────────────┐           ┌─────────────────────────────┐
        │   Redis Streams (tasks,     │ ◄──────── │   Redis Streams (billing,   │
        │   billing, ...channels)     │           │   tasks.result, ...)        │
        └─────────────────────────────┘           └─────────────────────────────┘
```

## Files
- `envelope.py`: Envelope dataclass and validation
- `bus.py`: Publish/subscribe helpers
- `tail.py`: CLI tool to view bus traffic

## Envelope Publishing Guidelines for MCP Bridge

When publishing an Envelope to the AG1 bus for MCP tool calls, ensure:

- **endpoint**: Always provide the full MCP base URL (e.g., `https://server.smithery.ai/@jmp0x7c00/my-echo-mcp`).  
  Do **not** use `@foo/bar` or add `/ws` or `/mcp/call/ws`—the bridge will handle protocol details.

- **tool**: The MCP tool name to invoke (e.g., `"echo"`, `"get_bot_info"`).

- **args**: Arguments for the tool, as a dict.

- **config**: Any required configuration for the MCP endpoint (e.g., tokens, API keys).

- **session_code**: Always include a unique session code for tracking and downstream correlation.

- **reply_to**: Set to `"mcp_bridge.outbox"` (or your preferred response channel).

**Example:**
```python
Envelope(
    role="user",
    content={
        "endpoint": "https://server.smithery.ai/@jmp0x7c00/my-echo-mcp",
        "tool": "echo",
        "args": {"message": "Hello, world!"},
        "config": {}
    },
    session_code="your-session-id-123",
    reply_to="mcp_bridge.outbox"
)
```

**Best Practices:**
- Use environment variables for secrets (tokens, API keys).
- Always set `session_code` for traceability.
- Handle error envelopes (with `envelope_type='error'`) in your consumers.
- For new endpoints, validate tool names and schemas before publishing.

---

## Agent Registration & Edge Integration

### Current Registration (Telegram Example)
- Each agent registers with the edge handler (e.g., Telegram) by sending a registration envelope to the `AG1:tg:register` stream.
- The envelope is constructed from a simple config file, e.g.:
  ```json
  {
    "agent_name": "Muse1",
    "tg_handle": "@AG1_muse_bot",
    "key": "..."
  }
  ```
- The registration envelope includes:
  - `agent_name`: The agent's unique name
  - `tg_handle`: The Telegram bot handle
  - `key`: The Telegram bot token

**Envelope Example:**
```python
Envelope(
    role="agent",
    envelope_type="register",
    agent_name=config["agent_name"],
    content={
        "tg_handle": config.get("tg_handle"),
        "key": config.get("key")
    },
    timestamp=...
)
```

### How It Works
- The Telegram edge handler listens for these and registers the agent for message routing.

---

### Future-Proofing: Supporting Multiple Edge Types

> **TODO: See `register_with_tg_handler` in agent_bus_minimal.py for implementation notes.**

- In the future, agents may need to register with various edge nodes (Telegram, Nostr, Matrix, Discord, etc.).
- The config should support a list of edges/channels:
  ```json
  {
    "agent_name": "Muse1",
    "edges": [
      {"type": "telegram", "handle": "@AG1_muse_bot", "key": "..."},
      {"type": "nostr", "pubkey": "npub1..."},
      {"type": "matrix", "id": "@muse:matrix.org", "token": "..."}
    ]
  }
  ```
- The registration envelope should include the relevant edge config in its `content` field.
- Each edge handler should process only registrations for its type.
- This allows easy extensibility for new protocols and edge types.

---

### Developer Notes
- To add a new edge type:
  1. Extend the agent config with the new edge type and its required fields.
  2. Update the registration logic to send the correct envelope for each edge.
  3. Implement/extend the edge handler to process new edge registrations.
- See the `register_with_tg_handler` function in `agent_bus_minimal.py` for a template and TODO notes.

### Agent Registry & Handshake
`BusAdapterV2` registers its `agent_id` in a Redis set (`AG1:registry:agents`) when started
and removes it on shutdown.  This provides a simple mechanism to ensure IDs are
unique on the network. Other components can check registration status using
`is_registered` from `AG1_AetherBus.agent_registry`.


---

### Experimental Attestation Layer
An optional proof-of-concept signing system lives under `feature/experimental_attestation`. It provides helper functions to sign and verify envelopes using HMAC and stores attestations in a local SQLite ledger. The bus itself is unchanged; use the provided `publish_with_attestation` and `subscribe_with_attestation` helpers if you wish to enable this layer.
