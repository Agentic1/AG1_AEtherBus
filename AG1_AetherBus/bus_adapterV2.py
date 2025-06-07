import asyncio, time
from typing import Callable, Dict, List
import sys
import uuid
from AG1_AetherBus.agent_bus import AgentBus  # core AgentBus engine
from AG1_AetherBus.bus import publish_envelope  # low-level xadd helper
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
import json
# Registry helpers for simple handshake
from AG1_AetherBus.agent_registry import register_agent, unregister_agent
# Redis specific imports
from redis.asyncio import Redis as AsyncRedis # For type hinting and explicit async Redis client
from redis.exceptions import ConnectionError as RedisConnectionError # For specific exception handling


from redis.asyncio import Redis as AsyncRedisClient

class BusAdapterV2:
    """
    Unified BusAdapter using the minimal AgentBus functions.

    Provides async add/remove subscriptions, publish, and introspection.
    Handlers are registered per-pattern and invoked with (env, redis).
    """
    def __init__(
        self,
        agent_id: str,
        core_handler: Callable[[Envelope, AsyncRedis], None],
        redis_client : AsyncRedis,
        patterns: List[str] = None,
        group: str = None
    ):
        self.agent_id = agent_id
        self.core     = core_handler
        self.redis : AsyncRedis    = redis_client
        self.group    = group or agent_id
        self.patterns = patterns or []
        # pattern -> handler mapping
        self._registry: Dict[str, Callable] = {}
        self._running_subscription_tasks: Dict[str, asyncio.Task] = {}

    async def start(self):
        """
        Subscribe to all statically configured patterns.
        """
        # ensure agent is registered before listening
        await register_agent(self.redis, self.agent_id)
        for pattern in self.patterns:
            await self._subscribe_pattern(pattern, self.core)

    async def stop(self):
        """Cancel all running subscription tasks."""
        for pattern in list(self._running_subscription_tasks.keys()):
            await self.remove_subscription(pattern)
        await unregister_agent(self.redis, self.agent_id)

    async def _subscribe_pattern(
        self,
        pattern: str,
        handler: Callable[[Envelope, any], None]
    ):
        """
        Internal: register handler and spawn a background subscribe loop.
        """
        # record handler
        self._registry[pattern] = handler

        # build a callback that normalizes data to Envelope
        async def callback(raw):
            if isinstance(raw, Envelope):
                env = raw
            else:
                env = Envelope.from_dict(raw)
            # invoke handler with (env, redis) or (env,)
            try:
                await handler(env, self.redis)
            except TypeError:
                await handler(env)

        # start the subscribe loop (never returns)
        task = asyncio.create_task(
            start_bus_subscriptions(
                redis=self.redis,
                patterns=[pattern],
                group=self.group,
                handler=callback
            )
        )
        self._running_subscription_tasks[pattern] = task

    async def add_subscription(
        self,
        pattern: str,
        handler: Callable[[Envelope, any], None]
    ):
        """
        Dynamically subscribe to a new pattern (returns immediately).
        """
        await self._subscribe_pattern(pattern, handler)

    async def remove_subscription(self, pattern: str):
        self._registry.pop(pattern, None)
        task_to_cancel = self._running_subscription_tasks.pop(pattern, None)
        if task_to_cancel:
            if not task_to_cancel.done():
                print(f"[BusAdapterV2] Attempting to cancel task for pattern '{pattern}' (task: {id(task_to_cancel)})...")
                task_to_cancel.cancel()
                try:
                    # Wait for the task to actually finish after cancellation request
                    # This allows its internal try/except/finally blocks for CancelledError to run
                    await asyncio.wait_for(task_to_cancel, timeout=5.0) # Add a timeout
                    print(f"[BusAdapterV2] Subscription task for pattern '{pattern}' completed after cancellation request.")
                except asyncio.CancelledError:
                    print(f"[BusAdapterV2] Subscription task for pattern '{pattern}' successfully cancelled and awaited.")
                except asyncio.TimeoutError:
                    print(f"[BusAdapterV2] Timeout awaiting cancelled task for '{pattern}'. It might not have handled cancellation cleanly.")
                except RedisConnectionError: 
                    print(f"[BusAdapterV2] Redis connection was closed while awaiting cancelled task for '{pattern}'. This is usually okay during shutdown.")
                except Exception as e: 
                    print(f"[BusAdapterV2] Error awaiting cancelled task for '{pattern}': {type(e).__name__} - {e}")   

    def list_subscriptions(self) -> List[str]:
        """Return all currently registered patterns."""
        return list(self._registry.keys())

    async def publish(self, stream: str, env: Envelope):
        """
        Publish an Envelope to a Redis stream.
        """
        await publish_envelope(self.redis, stream, env)

    async def request_response(
        self,
        stream: str,
        req_env: Envelope,
        timeout: float = 5.0
    ) -> Envelope:
        """
        Send req_env to `stream` then await a single response on req_env.reply_to
        matching correlation_id. Returns the responding Envelope.
        """
        import uuid
        import asyncio
        # prepare reply_to and correlation_id
        reply_to = req_env.reply_to or f"{self.agent_id}:outbox"
        req_env.reply_to = reply_to
        req_env.correlation_id = req_env.correlation_id or str(uuid.uuid4())

        # use a Future for one-off reply
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        async def _collector(env: Envelope, redis_client):
            if env.correlation_id == req_env.correlation_id and not future.done():
                future.set_result(env)

        # subscribe to reply stream
        await self.add_subscription(reply_to, _collector)
        # publish request
        await self.publish(stream, req_env)
        # wait for response
        resp_env = await asyncio.wait_for(future, timeout)
        # clean up
        await self.remove_subscription(reply_to)
        return resp_env

    def dump_wiring(self) -> List[Dict[str, str]]:
        """
        Returns a list of dicts like:
          [{"pattern": "<stream>", "handler": "<handler name>"}…]
        """
        return [
            {"pattern": pat, "handler": getattr(h, "__name__", repr(h))}
            for pat, h in self._registry.items()
        ]

    async def wait_for_next_message(
        self,
        pattern: str,
        predicate: Callable[[Envelope], bool]=lambda e: True,
        timeout: float=60.0
        ) -> Envelope:
        """
        Subscribe once to `pattern`, collect the first Envelope where
        predicate(env) is True, then unsubscribe and return it.
        """
        print(f"[WaitForNext] raw‐subscribing once to '{pattern}'")

        last_id = "$"
        deadline = time.time() + timeout

        while True:
            block = max(0, int((deadline - time.time())*1000))
            if block <= 0:
                print(f"[WaitForNext] timed out after {timeout}s")
                raise asyncio.TimeoutError

            # raw XREAD on *this* stream only
            result = await self.redis.xread({pattern: last_id}, block=block, count=1)
            if not result:
                continue

            for _, messages in result:
                for msg_id, fields in messages:
                    last_id = msg_id
                    raw = fields.get("data")
                    env = Envelope.from_dict(raw if isinstance(raw, dict) else json.loads(raw))
                    print(f"[WaitForNext] got raw envelope: {env}")
                    if predicate(env):
                        print(f"[WaitForNext] predicate passed, returning")
                        return env
