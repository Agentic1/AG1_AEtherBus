# agent_bus.py
from redis.asyncio import Redis
from AG1_AetherBus.bus import build_redis_url, ensure_group, subscribe
from AG1_AetherBus.keys import StreamKeyBuilder
import asyncio

from AG1_AetherBus.agent_bus_minimal import get_patterns, register_with_tg_handler
from AG1_AetherBus.bus import ensure_group, subscribe, build_redis_url
from AG1_AetherBus.keys import StreamKeyBuilder
from redis.asyncio import Redis
import asyncio

class AgentBus:
    @staticmethod
    def extract_envelope_fields(env):
        """
        Returns all fields of the envelope as a dict, supporting both objects and dicts.
        """
        if hasattr(env, "__dict__"):
            return dict(env.__dict__)
        elif isinstance(env, dict):
            return dict(env)
        else:
            # Fallback: try to extract common attributes
            return {attr: getattr(env, attr, None) for attr in dir(env) if not attr.startswith("__")}

    def __init__(self, agent_id, handler, redis_url=None, group=None, config=None):
        self.agent_id = agent_id
        self.handler = handler
        self.group = group or f"{agent_id}_agent"
        self.redis_url = redis_url or build_redis_url()
        self.redis = Redis.from_url(self.redis_url)
        self.config = config  # Pass config for registration
        self.patterns = get_patterns(agent_id)
        self.subscribed = set()

    async def start(self):
        # Register with tg handler before subscribing to bus
        await register_with_tg_handler(self.config, self.redis)
        print(f"[{self.agent_id}] Registered with TG handler.")

        async def handler_with_redis(env):
            await self.handler(env, self.redis)

        # Subscribe to all patterns (should only be inbox for minimal)
        await self.start_bus_subscriptions(
            redis=self.redis,
            patterns=self.patterns,
            group=self.group,
            handler=handler_with_redis
        )
        print(f"[{self.agent_id}] Subscribed to: {self.patterns}")
        await asyncio.Event().wait()

    async def start_bus_subscriptions(self, redis, patterns, group, handler):
        await asyncio.gather(*[
            self.discover_and_subscribe(redis, pattern, group, handler)
            for pattern in patterns
        ])

    async def discover_and_subscribe(self, redis, pattern, group, handler, poll_delay=5):
        while True:
            print(f"[{self.agent_id}] Scanning for: {pattern}")
            cursor = "0"
            while True:
                cursor, keys = await redis.scan(cursor=cursor, match=pattern)
                for key in keys:
                    key = key.decode() if isinstance(key, bytes) else key
                    if key not in self.subscribed:
                        print(f"[{self.agent_id}] Subscribing to stream: {key}")
                        await ensure_group(redis, key, group)
                        asyncio.create_task(subscribe(redis, key, handler, group))
                        self.subscribed.add(key)
                if cursor == "0":
                    break
            await asyncio.sleep(poll_delay)
