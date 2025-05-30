#!/usr/bin/env python3
import os
import sys
import asyncio
import redis.asyncio as aioredis
from envelope import Envelope
#from bus_adapter import BusAdapter
from bus_adapterV2 import BusAdapterV2 as BusAdapter

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

async def test1_dynamic_subscribe(r):
    print("TEST 1: dynamic subscribe/unsubscribe…", end=" ")
    ev = asyncio.Event()

    async def handler(env,redis_client):
        print(f'T1 handler')
        ev.set()

    adapter = BusAdapter(
        agent_id="DynTest",
        core_handler=handler,
        redis_client=r,
        patterns=[],
        group="DynGroup"
    )
    listener = asyncio.create_task(adapter.start())
    print(f'T1 adapter start')
    await asyncio.sleep(0.2)

    # 1) start dynamic subscription (returns immediately)
    print(f'T1 add sub')
    await adapter.add_subscription("test1:inbox", handler)
    await asyncio.sleep(0.6)
    # 2) publish (this now actually hits a live subscription)
    print(">> About to publish…")
    await adapter.publish("test1:inbox", Envelope(
        role="user", content={"msg":"1"}, user_id="TestAgent"
    ))
    print(f'T1 published')

    # 3) wait for it
    await asyncio.wait_for(ev.wait(), timeout=1)
    print("PASS")

    listener.cancel()

async def test2_list_subscriptions(r):
    print("TEST 2: list_subscriptions…", end=" ")
    async def noop(env, redis_client):
        pass

    # 1) static patterns on init
    static = ["a:1", "b:2"]
    adapter = BusAdapter(
        agent_id="ListTest",
        core_handler=noop,
        redis_client=r,
        patterns=static,
        group="ListGroup"
    )
    await adapter.start()
    await asyncio.sleep(0.2)

    got = set(adapter.list_subscriptions())
    if got == set(static):
        print("PASS (static)")
    else:
        print("FAIL (static) →", got); return

    # 2) dynamic add
    await adapter.add_subscription("c:3", noop)
    await asyncio.sleep(0.1)
    got2 = set(adapter.list_subscriptions())
    if got2 == set(static) | {"c:3"}:
        print("PASS (add)")
    else:
        print("FAIL (add) →", got2); return

    # 3) dynamic remove
    await adapter.remove_subscription("a:1")
    await asyncio.sleep(0.1)
    got3 = set(adapter.list_subscriptions())
    if got3 == (set(static) | {"c:3"}) - {"a:1"}:
        print("PASS (remove)")
    else:
        print("FAIL (remove) →", got3)



async def test3_request_response(r):
    print("TEST 3: request_response (RPC)…", end=" ")

    # Echo handler: replies back the same ping → pong
    async def echo(env, redis_client):
        print("[RPC] echo received ping:", env.content["ping"])
        resp = Envelope(
            role="agent",
            content={"pong": env.content["ping"]},
            user_id=env.user_id,
            correlation_id=env.correlation_id
        )
        print("[RPC] echo publishing to:", env.reply_to)
        await adapter.publish(env.reply_to, resp)
        
    # 1) Build & start adapter
    adapter = BusAdapter(
        agent_id="RPCTest",
        core_handler=echo,
        redis_client=r,
        patterns=["rpc:inbox"],
        group="RPCGroup"
    )
    await adapter.start()
    await asyncio.sleep(0.2)

    # 2) Issue RPC
    req = Envelope(role="user", content={"ping": 123}, user_id="u1")
    resp = await adapter.request_response("rpc:inbox", req, timeout=1.0)

    # 3) Assert
    if resp.content.get("pong") == 123:
        print("PASS")
    else:
        print("FAIL →", resp.content)

async def test4_ordering(r):
    print("TEST 4: ordering & backlog…", end=" ")
    ev = asyncio.Event()
    buffer = []

    # Handler that appends its incoming payload ID to buffer
    async def h(env, redis_client):
        buffer.append(env.content["id"])
        if len(buffer) == 5:
            ev.set()

    adapter = BusAdapter(
        agent_id="OrdTest",
        core_handler=h,
        redis_client=r,
        patterns=["ord:in"],
        group="OrdGroup"
    )
    await adapter.start()
    await asyncio.sleep(0.2)

    # 1) publish 5 messages quickly
    for i in range(5):
        await adapter.publish("ord:in", Envelope(role="user", content={"id": i}, user_id="ord"))
    # 2) wait for all to arrive
    await asyncio.wait_for(ev.wait(), timeout=1.0)

    # 3) assert order
    if buffer == list(range(5)):
        print("PASS")
    else:
        print("FAIL →", buffer)



async def test5_mix_minimal_group(r):
    print("TEST 5: minimal vs group", end="… ")
    rec_min = []
    rec_grp = []

    # handler for minimal
    async def hmin(env):
        rec_min.append(env.content["val"])
    # handler for group
    async def hgrp(env):
        rec_grp.append(env.content["val"])

    # minimal adapter (no group)
    A_min = BusAdapter("MixMin", hmin, r, patterns=["mix:in"], group="MixGroup")
    # group adapter
    A_grp = BusAdapter("MixGrp", hgrp, r, patterns=["mix:in"], group="MixGroup")

    t1 = asyncio.create_task(A_min.start())
    t2 = asyncio.create_task(A_grp.start())
    await asyncio.sleep(0.2)

    # publish 10 messages
    for i in range(10):
        await A_min.publish("mix:in", Envelope(role="user", content={"val": i}))
    await asyncio.sleep(1)

    # minimal should see all 10, group ~some (load-balanced)
    if len(rec_min) == 10 and 0 < len(rec_grp) < 10:
        print("PASS")
    else:
        print(f"FAIL → min={len(rec_min)} grp={len(rec_grp)}")
    t1.cancel(); t2.cancel()

async def test6_failure_retry(r):
    print("TEST 6: failure & retry", end="… ")
    ev = asyncio.Event()
    state = {"first": True}

    async def flaky(env):
        if state["first"]:
            state["first"] = False
            raise RuntimeError("boom")
        ev.set()

    adapter = BusAdapter("Flaky", flaky, r, patterns=["flk:in"], group="FlkGroup")
    t = asyncio.create_task(adapter.start())
    await asyncio.sleep(0.2)

    await adapter.publish("flk:in", Envelope(role="user", content={"x": 1}))
    try:
        await asyncio.wait_for(ev.wait(), timeout=2)
        print("PASS")
    except asyncio.TimeoutError:
        print("FAIL (no retry)")
    t.cancel()

async def test7_introspection(r):
    print("TEST 7: introspection", end="… ")
    async def hi(env): pass
    adapter = BusAdapter("Intros", hi, r, patterns=["i:1","i:2"], group="IntGroup")
    t = asyncio.create_task(adapter.start())
    await asyncio.sleep(0.2)
    dump = adapter.dump_wiring()
    keys = {d["pattern"] for d in dump}
    if keys == {"i:1","i:2"}:
        print("PASS")
    else:
        print("FAIL →", dump)
    t.cancel()

async def test_wait_for_next_message(r):
    print("TEST 8: wait_for_next_message…", end=" ")

    # 1) no-op core handler (we won't use it here)
    async def noop(env, redis_client):
        pass

    adapter = BusAdapter(
        agent_id="WaitTest",
        core_handler=noop,
        redis_client=r,
        patterns=[],            # dynamic only
        group="WaitGroup"       # consumer-group mode
    )
    await adapter.start()
    await asyncio.sleep(0.1)  # let the subscription loop spin up

    # 2) schedule a delayed publish into "wait:test"
    async def delayed_pub():
        await asyncio.sleep(0.1)
        await adapter.publish(
            "wait:test",
            Envelope(role="user", content={"msg": "hello"}, user_id="u1")
        )

    asyncio.create_task(delayed_pub())

    # 3) await the next message on "wait:test"
    try:
        env = await adapter.wait_for_next_message("wait:test", timeout=1.0)
    except asyncio.TimeoutError:
        print("FAIL (timed out)")
        return

    # 4) verify
    if env.content.get("msg") == "hello":
        print("PASS")
    else:
        print("FAIL →", env.content)

async def main():
    r = aioredis.from_url(REDIS_URL)
    await asyncio.sleep(0.1)
    print('t1')
    await test1_dynamic_subscribe(r)

    print('t2')
    await test2_list_subscriptions(r)
    
    print('t3')
    await test3_request_response(r)
    
    print('t4')
    await test4_ordering(r)
    
    print('t5')
    await test5_mix_minimal_group(r)
    print('t6')
   
    await test6_failure_retry(r)
    print('t7')
    await test7_introspection(r)
    print('t8')
    await test_wait_for_next_message(r)

if __name__ == "__main__":
    asyncio.run(main())
