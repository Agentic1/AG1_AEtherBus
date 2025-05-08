# agent_bus.py
from redis.asyncio import Redis
from AG1_AetherBus.bus import build_redis_url, ensure_group, subscribe
from AG1_AetherBus.keys import StreamKeyBuilder
import asyncio

class AgentBus:
    def __init__(self, agent_id, handler, redis_url=None, group=None):
        self.agent_id = agent_id
        self.handler = handler
        self.group = group or agent_id
        self.redis_url = redis_url or build_redis_url()
        self.redis = Redis.from_url(self.redis_url)
        self.keys = StreamKeyBuilder()
        self.subscribed = set()

    async def start(self):
        print(f"[{self.agent_id}] Starting...")
        inbox_key = self.keys.agent_inbox(self.agent_id)
        flow_key = f"AG1:flow:*input"  # can later resolve more smartly

        await self._subscribe_to(inbox_key)
        await self._discover_and_subscribe(flow_key)

    async def _subscribe_to(self, key):
        if key not in self.subscribed:
            await ensure_group(self.redis, key, self.group)
            asyncio.create_task(subscribe(self.redis, key, self.handler, self.group))
            self.subscribed.add(key)
            print(f"[{self.agent_id}] Subscribed to {key}")

    async def _discover_and_subscribe(self, pattern, delay=5):
        while True:
            print(f"[{self.agent_id}] Scanning for: {pattern}")
            cursor = "0"
            while True:
                cursor, keys = await self.redis.scan(cursor=cursor, match=pattern)
                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode()
                    await self._subscribe_to(key)
                if cursor == "0":
                    break
            await asyncio.sleep(delay)
