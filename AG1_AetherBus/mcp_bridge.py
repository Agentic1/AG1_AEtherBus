import asyncio
from dotenv import load_dotenv
load_dotenv()
import time
import json
import base64
import os
import redis.asyncio as aioredis

from AG1_AetherBus.bus import subscribe, publish_envelope, REDIS_HOST, REDIS_PORT
from AG1_AetherBus.envelope import Envelope

try:
    import mcp
    from mcp.client.websocket import websocket_client
except ImportError:
    print("[bus_to_mcp_bridge] ERROR: Please install the 'mcp' package (pip install mcp)")
    exit(1)

SMITHERY_API_KEY = os.getenv("SMITHERY_API_KEY", "61b21040-8b26-4030-8e6f-0dc07200dbd8")

def make_ws_url(endpoint, config, api_key):
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        base = endpoint.rstrip("/")
    elif endpoint.startswith("http://") or endpoint.startswith("https://"):
        base = "wss://" + endpoint.split("://", 1)[1].rstrip("/")
    else:
        base = f"wss://server.smithery.ai/{endpoint}".rstrip("/")
    ws_url = f"{base}/mcp/call/ws"
    config_b64 = base64.b64encode(json.dumps(config).encode()).decode()
    return f"{ws_url}?config={config_b64}&api_key={api_key}"

INBOX_CHANNEL = os.getenv("MCP_BRIDGE_INBOX", "mcp_bridge.inbox")
OUTBOX_CHANNEL = os.getenv("MCP_BRIDGE_OUTBOX", "mcp_bridge.outbox")

def make_json_safe(obj):
    if hasattr(obj, "to_dict"):
        return make_json_safe(obj.to_dict())
    elif isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    elif hasattr(obj, "__dict__"):
        return make_json_safe(vars(obj))
    else:
        return obj

async def process_envelope(env: Envelope, redis):
    print(f"[bus_to_mcp_bridge] Received Envelope: {env}")
    content = env.content or {}
    endpoint = content.get("endpoint")
    config = content.get("config", {})
    tool = content.get("tool")
    args = content.get("args", {})
    api_key = content.get("api_key") or SMITHERY_API_KEY
    reply_to = env.reply_to or OUTBOX_CHANNEL
    if not endpoint or not tool:
        print("[bus_to_mcp_bridge] ERROR: Envelope missing 'endpoint' or 'tool' in content")
        return
    try:
        try:
            result = await asyncio.wait_for(call_mcp(endpoint, config, tool, args, api_key), timeout=60)
            result_data = make_json_safe(result)
            env.add_hop("mcp_bridge")
            response_env = Envelope(
                role="system",
                content={"result": result_data},
                session_code=env.session_code,
                agent_name="bus_to_mcp_bridge",
                reply_to=None,
                envelope_type="mcp_result",
                headers={},
                meta={"source_envelope_id": env.envelope_id}
            )
            await publish_envelope(redis, reply_to, response_env)
            print(f"[bus_to_mcp_bridge] Published MCP result to {reply_to}")
        except asyncio.TimeoutError:
            print(f"[bus_to_mcp_bridge] ERROR: MCP call timed out! (envelope_id={env.envelope_id})")
            env.add_hop("mcp_bridge")
            error_env = Envelope(
                role="system",
                content={"error": "MCP call timed out"},
                session_code=env.session_code,
                agent_name="bus_to_mcp_bridge",
                reply_to=None,
                envelope_type="error",
                headers={},
                meta={"source_envelope_id": env.envelope_id}
            )
            await publish_envelope(redis, reply_to, error_env)
        except Exception as e:
            print(f"[bus_to_mcp_bridge] ERROR calling MCP: {e} (envelope_id={env.envelope_id})")
            env.add_hop("mcp_bridge")
            error_env = Envelope(
                role="system",
                content={"error": str(e)},
                session_code=env.session_code,
                agent_name="bus_to_mcp_bridge",
                reply_to=None,
                envelope_type="error",
                headers={},
                meta={"source_envelope_id": env.envelope_id}
            )
            await publish_envelope(redis, reply_to, error_env)
    except Exception as e:
        print(f"[bus_to_mcp_bridge] ERROR: {e}")

async def call_mcp(endpoint, config, tool, args, api_key):
    ws_url = make_ws_url(endpoint, config, api_key)
    async with websocket_client(ws_url) as streams:
        async with mcp.ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return result

async def main():
    redis = await aioredis.from_url(build_redis_url())
    print(f"[bus_to_mcp_bridge] Subscribing to {INBOX_CHANNEL}")
    async def handle_envelope(env):
        await process_envelope(env, redis)
    await subscribe(redis, INBOX_CHANNEL, handle_envelope, group="mcp_bridge", consumer="mcp_bridge_consumer")
    await redis.aclose()

def build_redis_url():
    user = os.getenv("REDIS_USERNAME")
    pwd = os.getenv("REDIS_PASSWORD")
    host = REDIS_HOST
    port = REDIS_PORT
    if user and pwd:
        return f"redis://{user}:{pwd}@{host}:{port}"
    elif pwd:
        return f"redis://:{pwd}@{host}:{port}"
    else:
        return f"redis://{host}:{port}"

if __name__ == "__main__":
    asyncio.run(main())
