import asyncio
import json
import time
from typing import Dict

from redis.asyncio import Redis

from AG1_AetherBus.bus import build_redis_url, publish_envelope, subscribe
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

try:
    import websockets  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    websockets = None

keys = StreamKeyBuilder()
REGISTER_STREAM = keys.edge_register("nostr")
DEFAULT_RELAY = "wss://relay.damus.io"

registered_agents: Dict[str, dict] = {}


async def nostr_listen(redis: Redis, cfg: dict) -> None:
    """Listen for events mentioning our pubkey and publish them to the agent."""
    pubkey = cfg["pubkey"]
    relay = cfg.get("relay", DEFAULT_RELAY)
    if not websockets:
        print("[NOSTR_EDGE] websockets package not installed")
        return
    while True:
        try:
            async with websockets.connect(relay) as ws:  # type: ignore
                sub_id = f"sub_{pubkey[:8]}"
                req = [
                    "REQ",
                    sub_id,
                    {"kinds": [1], "tags": {"#p": [pubkey]}},
                ]
                await ws.send(json.dumps(req))
                while True:
                    msg = await ws.recv()
                    evt = json.loads(msg)
                    if evt and evt[0] == "EVENT" and len(evt) > 2:
                        data = evt[2]
                        content = data.get("content", "")
                        sender = data.get("pubkey")
                        env = Envelope(
                            role="user",
                            user_id=sender,
                            agent_name=cfg["agent_name"],
                            content={"text": content},
                            envelope_type="message",
                            reply_to=keys.edge_response("nostr", sender),
                        )
                        await publish_envelope(
                            redis, keys.agent_inbox(cfg["agent_name"]), env
                        )
        except Exception as e:
            print(f"[NOSTR_EDGE] Listen error {e}, reconnecting in 5s")
            await asyncio.sleep(5)


async def send_note(cfg: dict, to_pubkey: str, text: str) -> None:
    """Send a basic text note to the relay."""
    if not websockets:
        print("[NOSTR_EDGE] websockets package not installed")
        return
    relay = cfg.get("relay", DEFAULT_RELAY)
    try:
        async with websockets.connect(relay) as ws:  # type: ignore
            event = {
                "content": text,
                "pubkey": cfg["pubkey"],
                "created_at": int(time.time()),
                "kind": 1,
                "tags": [["p", to_pubkey]],
            }
            await ws.send(json.dumps(["EVENT", event]))
    except Exception as e:
        print(f"[NOSTR_EDGE] Failed to send note: {e}")


async def handle_agent_reply(env: Envelope, cfg: dict) -> None:
    content = ""
    if isinstance(env.content, dict):
        content = env.content.get("text") or ""
    else:
        content = str(env.content)
    if not content:
        return
    to_pubkey = env.user_id or cfg.get("pubkey")
    if to_pubkey:
        await send_note(cfg, to_pubkey, content)


async def handle_register(env: Envelope, redis: Redis) -> None:
    if env.envelope_type != "register":
        return
    cfg = env.content or {}
    pubkey = cfg.get("pubkey")
    if not pubkey:
        print("[NOSTR_EDGE] Registration missing pubkey")
        return
    cfg["agent_name"] = env.agent_name
    registered_agents[pubkey] = cfg
    asyncio.create_task(nostr_listen(redis, cfg))
    asyncio.create_task(
        subscribe(
            redis,
            keys.edge_response("nostr", pubkey),
            lambda e: handle_agent_reply(e, cfg),
            group=f"nostr_edge_{pubkey}",
        )
    )
    print(f"[NOSTR_EDGE] Registered agent {env.agent_name} for pubkey {pubkey}")


async def main() -> None:
    redis = Redis.from_url(build_redis_url())
    await start_bus_subscriptions(
        redis=redis,
        patterns=[REGISTER_STREAM],
        group="nostr_edge",
        handler=lambda env: handle_register(env, redis),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
