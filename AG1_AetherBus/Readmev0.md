# BusAdapterV2 for AG1\_AetherBus

A developer-friendly, **consumer-group only** wrapper around AG1\_AetherBus stream primitives.
It provides async APIs for:

* **Publishing** messages via Redis XADD
* **Subscribing** to streams with XREADGROUP (consumer-group load balancing)
* **Dynamic subscriptions** at runtime
* **RPC-style** request/response
* **Introspection** of current wiring

> **Note:** By design, BusAdapterV2 uses only **consumer-group** mode for all handlers.
> For raw XREAD (see-everything) use the minimal-bus helper directly in dev tools.

---

## Table of Contents

1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [API Reference](#api-reference)
4. [Examples](#examples)
5. [Architecture Diagram](#architecture-diagram)
6. [Advanced Topics](#advanced-topics)
7. [Critique & What's Missing](#critique--whats-missing)

---

## Installation

```bash
# in your virtualenv
pip install ag1-aetherbus
# copy bus_adapterV2.py into your project
```

---

## Quick Start

```python
import asyncio
import redis.asyncio as aioredis
from AG1_AetherBus.envelope import Envelope
from bus_adapterV2 import BusAdapterV2

async def main():
    # Create Redis client
    r = aioredis.from_url("redis://localhost:6379/0")

    # Define your handler (consumer-group semantics)
    async def handler(env, redis_client):
        print("Processed message:", env.content)

    # Instantiate adapter
    adapter = BusAdapterV2(
        agent_id="MyAgent",
        core_handler=handler,
        redis_client=r,
        patterns=["myevent:in"],  # streams to subscribe at startup
        group="MyAgentGroup"      # consumer-group name
    )

    # Start subscriptions (runs in background)
    await adapter.start()

    # Publish a message
    await adapter.publish(
        "myevent:in",
        Envelope(role="user", content={"foo": 42})
    )

    # Keep running
    await asyncio.sleep(5)

asyncio.run(main())
```

---

## API Reference

### `__init__(agent_id, core_handler, redis_client, patterns=None, group)`

* **`agent_id`** (str): Unique identifier for this adapter.
* **`core_handler`** (callable): `async def handler(env: Envelope, redis_client)`.
* **`redis_client`**: `redis.asyncio.Redis` instance.
* **`patterns`** (List\[str], optional): Streams to subscribe on `start()`.
* **`group`** (str): Redis consumer-group name (required).
  All subscriptions use XREADGROUP with this group.

### `start() -> None`

Subscribe to all `patterns` via consumer-group XREADGROUP. Returns immediately; loops run in background.

### `add_subscription(pattern: str, handler) -> None`

Dynamically subscribe to a new stream pattern using XREADGROUP. Returns immediately.

### `remove_subscription(pattern: str) -> None`

Stop dispatching to the handler for `pattern`. Underlying loop still runs until shutdown.

### `list_subscriptions() -> List[str]`

List of all active patterns.

### `publish(stream: str, env: Envelope) -> None`

Publish an Envelope to `stream` via XADD.

### `request_response(stream: str, req_env: Envelope, timeout: float=5.0) -> Envelope`

RPC helper:

1. Sets up one-off consumer on `req_env.reply_to`.
2. Publishes `req_env`.
3. Waits for a matching `correlation_id` reply.
4. Unsubscribes and returns the response.

### `dump_wiring() -> List[Dict[str,str]]`

Inspect current wiring:

```python
[
  {"pattern": "myevent:in", "handler": "handler"},
  …
]
```

---

## Examples

### Dynamic Subscribe & Publish

```python
await adapter.add_subscription("orders:created", order_handler)
await adapter.publish(
    "orders:created",
    Envelope(role="user", content={"order_id": 123})
)
```

### RPC Call

```python
req = Envelope(role="user", content={"ping":1})
resp = await adapter.request_response("service:ping", req)
print("pong=", resp.content.get("pong"))
```

### Introspection

```python
print(adapter.list_subscriptions())
print(adapter.dump_wiring())
```

---

## Architecture Diagram

```
         +---------------+
         |  Redis Server  |
         +---------------+
         | Streams        |
         | myevent:in     |
         | myevent:out    |
         +---------------+
              ^     ^
              |     |
              |     +-- XADD (publish)
              |
          XREADGROUP
              |
    +---------+----------+
    |   BusAdapterV2     |
    | (consumer-group)   |
    +--------------------+
    | add_subscription   |
    | publish            |
    | request_response   |
    | list_subscriptions |
    | dump_wiring        |
    +---------+----------+
              |
              v
       Agent Instances
```

---

## Advanced Topics

### `wait_for_next_message(pattern: str, predicate: Callable[[Envelope], bool]=lambda e: True, timeout: float=60.0) -> Envelope`
Raw-XREAD helper: waits for the next Envelope on `pattern` that satisfies `predicate`, or raises `TimeoutError`.  

### Edge Connectors & TG Handler

BusAdapterV2 focuses on core agent wiring via Redis consumer-groups.
Edge connectors (Telegram, WebRelay, Nostr) should run in separate processes:

```python
# In your tg_edge_handler.py
start_bus_subscriptions(
  redis=…, patterns=["telegram:in"], group=None,
  handler=handle_telegram_event
)
```

**Registration** (via `register_with_tg_handler`) can happen once at startup in each connector process.

### Dev TUI & Raw Reads

For real-time debugging, bypass the adapter and use raw XREAD:

```python
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
start_bus_subscriptions(
  redis=…, patterns=["myevent:*"] , group=None,
  handler=your_debug_callback
)

env = await adapter.wait_for_next_message(
    pattern=f"AG1:agent:{agent_id}:inbox",
    predicate=lambda e: e.role=="user" and session_active.get(e.user_id, False),
    timeout=30.0
)
```

---

## Critique & What's Missing

1. **Graceful Shutdown**: no `.stop()` to cancel loops and close Redis cleanly.
2. **Health Checks**: no built-in liveness/probes for agent or connectors.
3. **Metrics & Tracing**: no instrumentation hooks for Prometheus/OpenTelemetry.
4. **Retry Logic**: consumer-group pending entries not auto-claimed; consider auto-claim or custom retries.
5. **Schema Validation**: no JSON schema enforcement; integrate validation for payload safety.

Consider adding these to make your bus integration production-grade.
