# AG1 Core Bus – Advanced Agent Integration Guide

(Moved to docs/ from project root.)

This guide covers advanced usage patterns for the AG1 core bus, including dynamic stream discovery, robust error handling, multi-agent orchestration, and production best practices. Use this as a supplement to `README_1stuse.md` for building scalable, reliable, and maintainable agentic services.

---

## 1. **Advanced Subscription Patterns**

### a. **Dynamic Discovery & Subscription**
- Use wildcard patterns and periodic scanning to subscribe to new streams as they appear (e.g., user inboxes, session flows).
- Example:
```python
from core_bus.bus import subscribe
from core_bus.keys import StreamKeyBuilder
import asyncio

async def dynamic_subscribe(redis, pattern, handler, group="agent_group", poll_delay=5):
    subscribed = set()
    while True:
        cursor = "0"
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern)
            for key in keys:
                if isinstance(key, bytes):
                    key = key.decode()
                if key not in subscribed:
                    await subscribe(redis, key, handler, group)
                    subscribed.add(key)
            if cursor == "0":
                break
        await asyncio.sleep(poll_delay)
```

### b. **Multiple Consumer Groups**
- Assign different consumer groups to different agent roles for sharding or redundancy.

---

## 2. **Robust Error Handling**

- Use try/except in your handler to catch and log errors without crashing the agent.
- Leverage the dead-letter logic in `core_bus.bus` for messages that fail repeatedly.
- Example:
```python
async def handler(env, redis):
    try:
        # ... process envelope ...
    except Exception as e:
        print(f"[ERROR] Failed to process envelope: {e}")
```

---

## 3. **Graceful Shutdown & Resource Cleanup**

- Always close your Redis connection on shutdown:
```python
try:
    await main()
finally:
    await redis.aclose()
```
- Use signal handlers for production agents to catch SIGTERM/SIGINT.

---

## 4. **Advanced Publishing**

- Use custom stream keys for targeted routing (see `core_bus/keys.py`).
- Attach metadata, correlation IDs, and trace hops for observability.

---

## 5. **Security & Auth**

- Use environment variables for all Redis credentials.
- Never hardcode secrets in code or scripts.
- For multi-tenant setups, consider stream-level access controls.

---

## 6. **Monitoring & Observability**

- Add logging for all bus events (subscribe, publish, error, etc.).
- Use the tail tool in `core_bus` to monitor live and backlog traffic.
- Integrate with external monitoring (e.g., Prometheus, ELK) as needed.

---

## 7. **Production Best Practices**

- Run agents under a process manager (e.g., systemd, supervisord, Docker).
- Use health checks and restart policies.
- Keep your dependencies up to date and pin versions in `pyproject.toml` or `requirements.txt`.

---

## 8. **Extending the Bus**

- Add new key patterns in `core_bus/keys.py` for custom flows.
- Write additional utilities for metrics, auditing, or integration with other event systems.

---

## 9. **Further Reading**
- See the code comments in `core_bus/` for more examples and API details.
- Review the AG1 project documentation for architecture and design patterns.

---

**Build robust, scalable, and maintainable agents with AG1 Core Bus!**
