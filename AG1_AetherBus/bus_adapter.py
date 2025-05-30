import asyncio
from typing import Callable, Dict, List
from agent_bus_minimal import start_bus_subscriptions, publish_envelope
from agent_bus import AgentBus
from envelope import Envelope

class BusAdapter:
    """
    Compact adapter integrating minimal and full AgentBus.

    :param agent_id: unique identifier for this agent
    :param core_handler: async function to handle incoming Envelope
    :param redis_client: pre-configured Redis client (optional)
    :param patterns: list of stream patterns to subscribe at startup
    :param group: consumer-group name (defaults to agent_id)
    :param full_bus: AgentBus class or subclass for group subscriptions
    """
    def __init__(
        self,
        agent_id: str,
        core_handler: Callable[[Envelope], None],
        redis_client=None,
        patterns: List[str]=None,
        group: str=None,
        full_bus: Callable=AgentBus
    ):
        self.agent_id = agent_id
        self.core_handler = core_handler
        # Redis client: use provided or expect calling code to build and pass one
        if redis_client:
            self.redis = redis_client
        else:
            raise ValueError("redis_client must be provided to BusAdapter")
        self.patterns = patterns or []
        self.group = group or agent_id
        # full AgentBus for consumer-group subscriptions
        self.full = full_bus(agent_id, handler=self._dispatch, config={})
        # registry mapping stream pattern to handler
        self._registry: Dict[str, Callable] = {}

    async def start(self):
        """
        Perform startup subscriptions on both minimal and full buses.
        """
        print(f'BusAdapter: Start patterns: {self.patterns}')
        # minimal xread subscriptions
        if self.patterns:
            await start_bus_subscriptions(
                redis=self.redis,
                patterns=self.patterns,
                group=self.group,
                handler=self._dispatch
            )
        # full consumer-group subscriptions
        await self.full.start()
        # register core handler
        for p in self.patterns:
            self._registry[p] = self.core_handler

    async def _dispatch(self, env: Envelope):
        """
        env is always an Envelope (subscribe() already parsed it for us).
        Just look up its stream and call the registered handler.
        """
        # 1) Which stream did this come from?
        #    The bus core injects the channel name into env.content['stream']
        #    on discovery, or env.reply_to on RPCs.
        stream = getattr(env, "reply_to", None)
        handler = self._registry.get(stream)
        if not handler:
            print(f"[BusAdapter] no handler for stream {stream!r}")
            return

        # 2) invoke in Muse style (env, redis) if supported, else fallback to (env,)
        try:
            await handler(env, self.redis)
        except TypeError:
            await handler(env)
            
    async def add_subscription(
        self,
        pattern: str,
        handler: Callable[[Envelope], None],
        use_group: bool=False
    ):
        """
        Dynamically subscribe to a new stream pattern.
        """
        """if use_group:
            await self.full.subscribe(pattern, self._dispatch)
        else:
            await start_bus_subscriptions(
                redis=self.redis,
                patterns=[pattern],
                group=self.group,
                handler=self._dispatch
            )
        self._registry[pattern] = handler"""

        # record it for introspection
        self._registry[pattern] = handler

        # 2) launch the subscriber in the background
        import asyncio
        if use_group:
            # consumer‐group subscriber (infinite loop) :contentReference[oaicite:0]{index=0}:contentReference[oaicite:1]{index=1}
            asyncio.create_task(self.full.subscribe(pattern, self._dispatch))
        else:
            # raw xread discovery loop (infinite loop) :contentReference[oaicite:2]{index=2}:contentReference[oaicite:3]{index=3}
            from agent_bus_minimal import discover_and_subscribe
            asyncio.create_task(
                discover_and_subscribe(self.redis, pattern, self.group, self._dispatch)
            )

    async def remove_subscription(self, pattern: str):
        """
        Remove a dynamic subscription (only via full AgentBus).
        """
        await self.full.unsubscribe(pattern)
        self._registry.pop(pattern, None)

    def list_subscriptions(self) -> List[str]:
        """Return currently subscribed stream patterns."""
        return list(self._registry.keys())

    async def publish(self, target_stream: str, envelope: Envelope):
        """
        Publish an Envelope to a target stream using minimal bus.
        """
        await publish_envelope(
            self.redis,
            target_stream,
            envelope,
            #envelope=envelope.to_dict()
        )

    async def request_response(
        self,
        target_stream: str,
        envelope: Envelope,
        timeout: float=10.0
    ) -> Envelope:
        """
        RPC-style send-and-wait for response.
        """
        envelope.reply_to = f"{self.agent_id}:outbox"
        envelope.correlation_id = envelope.correlation_id or Envelope.new_id()
        await self.publish(target_stream, envelope)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                raise TimeoutError(f"no reply from {target_stream}")
            msgs = await self.redis.xread(
                {envelope.reply_to: '>'}, count=1,
                block=int((deadline-now)*1000)
            )
            if msgs:
                _, entries = msgs[0]
                for _, data in entries:
                    resp = Envelope.from_dict(data)
                    if resp.correlation_id == envelope.correlation_id:
                        return resp

    def dump_wiring(self) -> List[Dict[str, str]]:
        """Inspect current pattern→handler mappings."""
        print(f'[Dumeiring]')
        return [
            {'pattern': p, 'handler': h.__qualname__}
            for p, h in self._registry.items()
        ]

# --- Basic Operational Test Script ---
# Run this to validate BusAdapter functionality and serve as a usage example.
# Requires a local Redis and a stream named 'test_stream:inbox'.
import os, asyncio
import redis.asyncio as aioredis
from envelope import Envelope
async def handler(env):
    print("✔️  Received:", env.stream, env.to_dict())

async def main():
    # 1. Build an async Redis client
    print('1')
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)
    print('2')
    # 2. Instantiate the adapter (no manual xadd needed)
    adapter = BusAdapter(
        agent_id="TestAgent",
        core_handler=handler,
        redis_client=r,
        patterns=["AG1:test_stream:inbox"],
        group="TestGroup"
    )
    print('3')
    # 3. Start subscriptions (now adapter is watching the stream)
    listen_task = asyncio.create_task(adapter.start())
    print('4')
    # 4. Publish via the adapter
    env = Envelope(
        role="user",                               # required
        content={"msg": "hello"},                  # your payload
        user_id="TestAgent",                       # optional metadata
    )
    print('5')
    await adapter.publish("AG1:test_stream:inbox", env)
    print('6')
    # Give it a moment to deliver
    await asyncio.sleep(1)

    # Inspect wiring
    print("Wiring:", adapter.dump_wiring())

    # Clean up
    # 8. Clean up: cancel the background listener
    listen_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass

asyncio.run(main())