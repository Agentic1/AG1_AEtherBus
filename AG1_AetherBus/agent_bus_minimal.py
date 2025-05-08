from AG1_AetherBus.bus import ensure_group, subscribe
import asyncio

current_subscriptions = set()

async def discover_and_subscribe(redis, pattern, group, handler, poll_delay=5):
    while True:
        print(f"[DISCOVERY] Scanning for: {pattern}")
        cursor = "0"
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern)
            for key in keys:
                key = key.decode() if isinstance(key, bytes) else key
                if key not in current_subscriptions:
                    print(f"[DISCOVERY] Subscribing to stream: {key}")
                    await ensure_group(redis, key, group)
                    asyncio.create_task(subscribe(redis, key, handler, group))
                    current_subscriptions.add(key)
            if cursor == "0":
                break
        await asyncio.sleep(poll_delay)

async def start_bus_subscriptions(redis, patterns, group, handler):
    await asyncio.gather(*[
        discover_and_subscribe(redis, pattern, group, handler)
        for pattern in patterns
    ])
