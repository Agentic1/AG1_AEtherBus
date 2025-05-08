

# relay_server_bus.py
# This version uses your internal Envelope and bus system
import aiohttp_cors
from aiohttp import web
import uuid
import asyncio
import json
from dotenv import load_dotenv
import redis.asyncio as aioredis
import os
from AG1_AetherBus.bus import publish_envelope, build_redis_url, subscribe
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.envelope import Envelope
# from AG1_AetherBus.utils import publish_to_resolved_stream  # Uncomment if needed

from aiohttp import web
import uuid

load_dotenv()

API_KEY = os.getenv("RELAY_API_KEY", "supersecret")
REDIS_URL = build_redis_url()
redis = aioredis.from_url(REDIS_URL)
#redis = Redis.from_url(build_redis_url())


# --- Send Route: Push user message to the bus using Envelope ---
async def send_message_oneway(request):
    print(f'[RELAY][Send] {request}')
    print(f'[RELAY][Send] headers: {dict(request.headers)}')
    print(f'[RELAY][Send] body: {await request.text()}')
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
        user_id = data.get("user_id")
        text = data.get("text")
        target_stream = data.get("target") or f"user.{user_id}.inbox"

        if not user_id or not text:
            return web.Response(status=400, text="Missing user_id or text")

        envelope = Envelope(
            role="user",
            user_id=user_id,
            reply_to=f"user.{user_id}.inbox.response",
            content={"text": text},
            agent_name="relay_server",
            envelope_type="message"
        )

        await publish_envelope(redis, target_stream, envelope)
        return web.json_response({"status": "sent", "target_stream": target_stream})

    except Exception as e:
        return web.Response(status=500, text=f"Error: {str(e)}")

import asyncio
from AG1_AetherBus.bus import subscribe

async def wait_for_reply(redis, correlation_id, reply_to, timeout=3):
    print(f"[RelayWaiter] Waiting for reply: correlation_id={correlation_id}, reply_to={reply_to}")
    q = asyncio.Queue()

    async def handler(envelope):
        print(f"[RelayWaiter] Received envelope on {reply_to}: {envelope}")
        if hasattr(envelope, "correlation_id") and envelope.correlation_id == correlation_id:
            print(f"[RelayWaiter] Match found for correlation_id={correlation_id}")
            print(f"[RelayWaiter] Match found for env={envelope}")
            
            try:
                result = envelope.to_dict()
                await q.put(result)
            except Exception as e:
                print(f"[RelayWaiter][ERROR] Could not queue envelope: {e}")

    # Start background subscriber task
    listener_task = asyncio.create_task(subscribe(redis, reply_to, handler))
    print(f"[RelayWaiter] Subscribed to {reply_to} — awaiting response...")

    try:
        reply = await asyncio.wait_for(q.get(), timeout=timeout)
        print(f"[RelayWaiter] Got reply! {reply}")
        return reply
    except asyncio.TimeoutError:
        print(f"[RelayWaiter] Timeout after {timeout}s waiting for reply_to={reply_to}")
        return None
    finally:
        print(f"[RelayWaiter] Cancelling background listener...")
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            print(f"[RelayWaiter] Listener task cancelled cleanly.")

async def send_message(request):
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
    except Exception as e:
        print(f"[RELAY][ERROR] Could not parse JSON: {e}")
        return web.Response(status=400, text="Invalid JSON body")

    try:
        user_id = data.get("user_id", "unknown_user")
        text = data.get("text", "")
        session_code = data.get("session_code")
        agent_name = data.get("agent_name", "pa0")
        correlation_id = str(uuid.uuid4())
        kb = StreamKeyBuilder()

        print(f"[RELAY][Send] ➤ user_id={user_id}, agent_name={agent_name}, session_code={session_code}")

        if session_code:
            target_stream = kb.flow_input(session_code)
            reply_to = kb.flow_output(session_code)
            print(f"[RELAY][Target] Using session flow")
        else:
            target_stream = kb.agent_inbox(agent_name)
            reply_to = kb.user_inbox(user_id)
            print(f"[RELAY][Target] Using agent inbox: {target_stream}")

        print(f"[RELAY][ReplyTo] Will wait on: {reply_to}")
        print(f"[RELAY][CID] Correlation ID: {correlation_id}")

        envelope = Envelope(
            role="user",
            user_id=user_id,
            agent_name=agent_name,
            reply_to=reply_to,
            correlation_id=correlation_id,
            session_code=session_code,
            content={"text": text},
            envelope_type="message"
        )

        print(f"[RELAY][Envelope] ➤ {envelope.to_dict()}")
        await publish_to_resolved_stream(redis, envelope)
        print(f"[RELAY][Publish] Sent to stream: {target_stream}")

        # Wait for response
        
        result = await wait_for_reply(redis, correlation_id, reply_to, timeout=10)

        if result:
            return web.json_response(result)
        else:
            return web.json_response({
                "status": "sent",
                "correlation_id": correlation_id,
                "text": text,
                "user_id": user_id,
                "reply_to": reply_to,
                "target_stream": target_stream
            })

    except Exception as e:
        print(f"[RELAY][ERROR] {str(e)}")
        return web.Response(status=500, text=f"Internal error: {str(e)}")

# --- Poll Route: Get messages from user inbox or flow output ---
async def poll_messages(request):
    print('[POLLING] Incoming poll request')
    
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")

    user_id = request.query.get("user_id")
    since = request.query.get("since", "-")
    flow = request.query.get("flow") or request.query.get("session_code")

    kb = StreamKeyBuilder()

    if flow:
        stream = kb.flow_output(flow)
        print(f'[poll][stream] {stream}')
    elif user_id:
        stream = kb.user_inbox(user_id)
        print(f'[poll][user] {stream}')
    else:
        print(f'[Poll][Missing flow/user]')
        return web.Response(status=400, text="Missing flow or user_id")

    try:
        messages = await redis.xrange(stream, since, "+", count=100)
    except Exception as e:
        print(f"[POLL][ERROR] Could not read stream {stream}: {e}")
        return web.Response(status=500, text="Error reading stream")

    parsed = []
    for msg_id, entry in messages:
        try:
            data = json.loads(entry.get(b"data", b"{}").decode())
            data["_id"] = msg_id.decode()
            parsed.append(data)
        except Exception as e:
            print(f"[POLL][WARN] Failed to decode entry: {e}")
            continue

    return web.json_response(parsed)


# --- App Setup ---
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        # Preflight request
        return web.Response(status=200, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        })

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

app = web.Application(middlewares=[cors_middleware])

# CORS setup — allow everything for now
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
    )
})

app.router.add_post("/send", send_message)
app.router.add_get("/poll", poll_messages)

send_route = app.router.add_post("/send", send_message)
poll_route = app.router.add_get("/poll", poll_messages)

# --- Bind CORS to routes ---
cors.add(send_route)
cors.add(poll_route)

#app["redis"] = aioredis.from_url(build_redis_url())
print(f'[REDIS ] {build_redis_url}')
if __name__ == "__main__":
    web.run_app(app, port=4004)