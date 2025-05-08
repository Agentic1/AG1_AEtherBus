# AG1 Core Bus – Minimal Agent Integration (First Use Guide)

(Moved to docs/ from project root.)

Welcome to the AG1 Core Bus minimal agent integration! This guide explains how to use the `core_bus` package and the `agent_bus_minimal` connector to quickly build a new agent that subscribes to the bus and handles envelopes.

---

## 1. **Quickstart: Minimal Agent Setup**

### a. **Dependencies**

- Python 3.8+
- `redis.asyncio`
- `python-dotenv` (if using `.env` files for config)

Install dependencies:
```sh
pip install redis python-dotenv
```

### b. **Import the Bus Connector**

In your agent’s main Python file (e.g., `pa0.py`):

```python
from core_bus.agent_bus_minimal import start_bus_subscriptions
from core_bus.keys import StreamKeyBuilder
from core_bus.bus import build_redis_url, publish_envelope
import redis.asyncio as aioredis

# Define your envelope handler
async def handle_bus_envelope(env, redis):
    print(f"Received envelope: {env}")
    # Example: Echo the message back
    if env.reply_to:
        await publish_envelope(redis, env.reply_to, env)

async def main():
    redis = aioredis.from_url(build_redis_url())
    kb = StreamKeyBuilder()
    patterns = [kb.agent_inbox("*"), kb.flow_input("*")]

    async def handler_with_redis(env):
        await handle_bus_envelope(env, redis)

    await start_bus_subscriptions(
        redis=redis,
        patterns=patterns,
        group="my_agent_group",
        handler=handler_with_redis
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

---

## 2. **How It Works**

- `start_bus_subscriptions` will:
    - Discover all matching streams (e.g., `AG1:agent:pa0:inbox`, `AG1:flow:XYZ:input`).
    - Subscribe to them using a Redis consumer group.
    - Call your handler on every new envelope/message.

- Your handler receives:
    - `env`: The parsed Envelope object.
    - `redis`: The Redis connection (for replying or further bus actions).

---

## 3. **Customizing Your Agent**

- Change the `patterns` list to subscribe to different streams (see `core_bus/keys.py` for helpers).
- Write custom logic in `handle_bus_envelope` to process or route messages.
- Use `publish_envelope` to send new messages or replies.

---

## 4. **Troubleshooting**

- **Malformed envelope errors:**  
  Use `XRANGE <stream> - +` in `redis-cli` to inspect raw messages.
- **No messages received:**  
  Confirm your agent is subscribing to the correct pattern and group.
- **Redis connection issues:**  
  Check your environment variables and `build_redis_url()` usage.

---

## 5. **Next Steps**

- Extend your agent logic as needed.
- Copy the `core_bus` package to any new project for instant bus connectivity.
- For advanced use, see the full AG1 documentation or the code comments in `core_bus/`.

---

**Enjoy rapid agent development with AG1 Core Bus!**
