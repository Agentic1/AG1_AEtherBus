import argparse
import asyncio
import os

import aiohttp_cors
from aiohttp import web
from dotenv import load_dotenv
import redis.asyncio as aioredis

from AG1_AetherBus.bus import build_redis_url, subscribe
from AG1_AetherBus.keys import StreamKeyBuilder

load_dotenv()

API_KEY = os.getenv("RELAY_API_KEY", "supersecret")
REDIS_URL = build_redis_url()
REQUIRE_WS_API_KEY = os.getenv("REQUIRE_WS_API_KEY", "false").lower() == "true"

# Shared key builder
kb = StreamKeyBuilder()

ALLOWED_ORIGINS = ["*"]

from .registrations import handle_aetherdeck_registration_envelope
from .websocket import websocket_handler
from .http_routes import send_message_oneway, send_message, poll_messages


async def on_startup_redis(app: web.Application):
    print(f"[APP-LIFECYCLE] Connecting to Redis at {REDIS_URL}")
    app['redis_pool'] = await aioredis.from_url(REDIS_URL, decode_responses=True)
    print("[APP-LIFECYCLE] Redis connection pool established.")


async def on_cleanup_redis(app: web.Application):
    if 'redis_pool' in app and app['redis_pool']:
        print("[APP-LIFECYCLE] Closing Redis connection pool...")
        await app['redis_pool'].close()
        print("[APP-LIFECYCLE] Redis connection pool closed.")


async def cors_middleware(request: web.Request, handler):
    request_origin = request.headers.get("Origin")
    allow_origin_header = "*"
    if request_origin and request_origin in ALLOWED_ORIGINS:
        allow_origin_header = request_origin
    elif "*" in ALLOWED_ORIGINS:
        allow_origin_header = "*"
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": allow_origin_header,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS, PUT, DELETE, PATCH",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Max-Age": "86400",
        }
        return web.Response(status=200, headers=headers)

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = allow_origin_header
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE, PATCH"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def create_app() -> web.Application:
    print("[APP] Creating aiohttp application and registering routes")
    app = web.Application(middlewares=[cors_middleware])

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*",
        )
    })

    ws_route = app.router.add_get("/", websocket_handler)
    http_send_oneway_route = app.router.add_post("/send_oneway", send_message_oneway)
    http_send_route = app.router.add_post("/send", send_message)
    http_poll_route = app.router.add_get("/poll", poll_messages)

    for route in [ws_route, http_send_oneway_route, http_send_route, http_poll_route]:
        cors.add(route)

    print("[APP] Routes registered: / (ws), /send_oneway, /send, /poll")
    return app


async def start_all_services():
    app = create_app()
    app.on_startup.append(on_startup_redis)
    app.on_cleanup.append(on_cleanup_redis)

    runner = web.AppRunner(app)
    await runner.setup()
    redis_for_subscriptions = app['redis_pool']

    registration_patterns = [kb.edge_register("aetherdeck")]
    print(f"[AETHERDECK_HANDLER][INIT] Subscribing to agent registration stream: {registration_patterns}")
    for stream_pattern in registration_patterns:
        asyncio.create_task(
            subscribe(
                redis_for_subscriptions,
                stream_pattern,
                lambda env, rc=redis_for_subscriptions: asyncio.create_task(handle_aetherdeck_registration_envelope(env, rc)),
                group="aetherdeck_registration_listener",
            )
        )

    await asyncio.sleep(0.5)
    listen_port = int(os.getenv("RELAY_PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', listen_port)
    await site.start()

    print(f"[INIT] Starting AetherRelay Server...")
    print(f"[INIT] API_KEY loaded (first 5 chars): {API_KEY[:5]}...")
    print(f"[INIT] REQUIRE_WS_API_KEY: {REQUIRE_WS_API_KEY}")
    print(f"[INIT] Redis URL: {REDIS_URL}")
    print(f"[INIT] HTTP endpoints available at /send, /send_oneway, /poll")
    print(f"[INIT] WebSocket endpoint available at /")
    print(f"======== Running on http://0.0.0.0:{listen_port} ========")

    await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description="Start AetherDeck Edge Handler (Relay).")
    parser.parse_known_args()
    try:
        asyncio.run(start_all_services())
    except KeyboardInterrupt:
        print("\n[AETHERDECK_HANDLER] Server shutting down (KeyboardInterrupt received).")
    except Exception as e:
        print(f"[AETHERDECK_HANDLER] Server failed to start or run: {e}")
        import traceback
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    main()
