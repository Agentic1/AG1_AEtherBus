import time
from typing import Optional, Dict
from redis.asyncio import Redis

REGISTRY_SET_KEY = "AG1:registry:agents"

async def register_agent(redis: Redis, agent_id: str, metadata: Optional[Dict[str, str]] = None) -> bool:
    """Register an agent ID in the global registry.

    Returns True if the ID was newly added, False if it already existed."""
    added = await redis.sadd(REGISTRY_SET_KEY, agent_id)
    if metadata is None:
        metadata = {}
    metadata.setdefault("registered_at", str(time.time()))
    if added:
        await redis.hset(f"AG1:registry:info:{agent_id}", mapping=metadata)
    return bool(added)

async def unregister_agent(redis: Redis, agent_id: str) -> None:
    """Remove the agent ID from the registry."""
    await redis.srem(REGISTRY_SET_KEY, agent_id)
    await redis.delete(f"AG1:registry:info:{agent_id}")

async def is_registered(redis: Redis, agent_id: str) -> bool:
    """Check if an agent ID is registered."""
    return bool(await redis.sismember(REGISTRY_SET_KEY, agent_id))
