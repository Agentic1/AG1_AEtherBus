# AG1 Core Bus – Agent Registry

The agent registry ensures each `agent_id` on the network is unique and allows other components to verify which agents are active.

`BusAdapterV2` automatically registers and unregisters its `agent_id` when `start()` and `stop()` are called. Registration information is stored in Redis so any node can check it.

## How It Works

1. **Registration** – On startup the adapter calls `register_agent(redis, agent_id)` which adds the ID to the Redis set `AG1:registry:agents` and stores basic metadata.
2. **Handshake** – When the adapter stops, `unregister_agent` removes the ID from the set.
3. **Lookup** – Other services call `is_registered(redis, agent_id)` to verify that an ID is present before trusting messages.

```
Agent start
   |
   | register_agent()
   v
+----------------------+     +-----------------------+
|   BusAdapterV2       | --> | AG1:registry:agents   |
+----------------------+     +-----------------------+
                                   ^
                                   |
                        is_registered(agent_id)
```

This lightweight handshake prevents accidental ID collisions and provides a simple way to see which agents are online.

## Example Usage

```python
from AG1_AetherBus.bus_adapterV2 import BusAdapterV2
from AG1_AetherBus.agent_registry import is_registered
from redis.asyncio import Redis

async def handler(env, redis):
    ...

redis = Redis.from_url("redis://localhost:6379")
adapter = BusAdapterV2("muse1", handler, redis, patterns=["AG1:agent:muse1:inbox"])
await adapter.start()  # automatically registers "muse1"

# Later we can check:
registered = await is_registered(redis, "muse1")
```

To run a local registry service or view current agents, simply query the Redis set `AG1:registry:agents` using any Redis client.
