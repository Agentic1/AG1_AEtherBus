# AG1 Core Bus – v1a

(Moved to docs/ from project root.)

Redis-based asynchronous message bus designed for envelope-based agent communication.

## 🚀 Overview

This system powers agent-to-agent, session-based, and edge-to-core communication using structured envelopes and namespaced Redis streams.

All communication happens through a single data structure: the `Envelope`.

## 📦 Key Concepts

- **Envelope:** The atomic unit of communication between agents
- **StreamKeyBuilder:** Generates standardized Redis stream keys
- **Bus:** Handles publish/subscribe using Redis consumer groups
- **AgentBus:** Drop-in agent listener with inbox/flow routing

## 🧠 Envelope Schema

```python
Envelope(
    role="user",
    content={"command": "echo"},
    session_code="abc123",
    agent_name="pa0",
    headers={"platform": "telegram"},
    ...
)
Includes support for meta, trace, billing_hint, tools_used, and more.

🔑 Key Conventions
All keys follow a namespaced pattern (AG1: default).

Type	Pattern
Agent Inbox	AG1:agent:{agent_id}:inbox
Agent Outbox	AG1:agent:{agent_id}:outbox
Session Stream	AG1:session:{session_code}:stream
Flow Input	AG1:flow:{flow_id}:input
Edge Register	AG1:edge:{platform}:register
User Inbox	AG1:user:{user_id}:inbox

Managed by StreamKeyBuilder.

🛠️ Core Functions
python
Copy
Edit
# Publish manually
await publish_envelope(redis, stream, envelope)

# Publish via envelope logic
await publish_to_resolved_stream(redis, envelope)

# Subscribe to a stream
await subscribe(redis, stream, callback, group="pa0")
⚙️ Agent Runtime Example
python
Copy
Edit
from agent_bus import AgentBus

async def handle_envelope(env):
    print("Got:", env.content)

bus = AgentBus(agent_id="pa0", handler=handle_envelope)
await bus.start()
🧪 Testing
bash
Copy
Edit
python test_send.py
python test_subscribe.py
Edit the files to set agent_id, session_code, or target stream.

🧼 TODO / Extensions
Memory-backed delivery queue

Intent routing by role/meta

Envelope versioning

NATS/Kafka interop later