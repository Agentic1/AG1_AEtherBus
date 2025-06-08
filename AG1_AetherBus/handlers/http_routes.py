import asyncio
import json
import uuid
from aiohttp import web

from AG1_AetherBus.bus import publish_envelope, subscribe
from AG1_AetherBus.envelope import Envelope

from .server import kb, API_KEY

async def send_message_oneway(request: web.Request):
    print(f'[RELAY][HTTP-SendOneWay] {request}')
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")
    try:
        data = await request.json()
        user_id = data.get("user_id")
        text = data.get("text")
        target_stream = data.get("target") or kb.user_inbox(user_id)
        if not user_id or not text:
            return web.Response(status=400, text="Missing user_id or text")
        envelope = Envelope(
            role="user",
            user_id=user_id,
            reply_to=kb.user_inbox_response(user_id),
            content={"text": text},
            agent_name="http_relay_oneway",
            envelope_type="message",
        )
        await publish_envelope(request.app['redis_pool'], target_stream, envelope)
        return web.json_response({"status": "sent", "target_stream": target_stream})
    except Exception as e:
        return web.Response(status=500, text=f"Error: {str(e)}")


async def wait_for_reply(redis_client, correlation_id: str, reply_to_stream: str, timeout: int = 3):
    print(f"[RelayWaiter] Waiting for reply: correlation_id={correlation_id}, reply_to={reply_to_stream}")
    q = asyncio.Queue()
    async def handler(envelope: Envelope):
        if hasattr(envelope, "correlation_id") and envelope.correlation_id == correlation_id:
            await q.put(envelope.to_dict())
    listener_task = asyncio.create_task(subscribe(redis_client, reply_to_stream, handler))
    try:
        reply = await asyncio.wait_for(q.get(), timeout=timeout)
        return reply
    except asyncio.TimeoutError:
        return None
    finally:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


async def send_message(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON body")

    user_id = data.get("user_id", "unknown_user")
    text = data.get("text", "")
    session_code = data.get("session_code")
    agent_name = data.get("agent_name", "pa0")
    correlation_id = str(uuid.uuid4())

    if session_code:
        target_stream = kb.flow_input(session_code)
        reply_to = kb.flow_output(session_code)
    else:
        target_stream = kb.agent_inbox(agent_name)
        reply_to = kb.user_inbox_response(user_id)

    envelope = Envelope(
        role="user",
        user_id=user_id,
        agent_name=agent_name,
        reply_to=reply_to,
        correlation_id=correlation_id,
        session_code=session_code,
        content={"text": text},
        envelope_type="message",
    )

    await publish_envelope(request.app['redis_pool'], target_stream, envelope)
    print(f"[RELAY][HTTP-Send] Sent to {target_stream}, waiting on {reply_to} (CID: {correlation_id})")
    result = await wait_for_reply(request.app['redis_pool'], correlation_id, reply_to, timeout=10)

    if result:
        return web.json_response(result)
    else:
        return web.json_response({
            "status": "sent_no_reply_within_timeout",
            "correlation_id": correlation_id,
            "text": text,
            "user_id": user_id,
            "reply_to_waited_on": reply_to,
            "target_stream": target_stream,
        }, status=202)


async def poll_messages(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        return web.Response(status=401, text="Unauthorized")
    user_id = request.query.get("user_id")
    since = request.query.get("since", "-")
    flow = request.query.get("flow") or request.query.get("session_code")

    if flow:
        stream_to_poll = kb.flow_output(flow)
    elif user_id:
        stream_to_poll = kb.user_inbox(user_id)
    else:
        return web.Response(status=400, text="Missing flow or user_id for polling")

    try:
        messages = await request.app['redis_pool'].xrange(stream_to_poll, since, "+", count=100)
        parsed = []
        for msg_id, entry in messages:
            try:
                data = json.loads(entry.get(b"data", b"{}").decode())
                data["_stream_message_id"] = msg_id.decode()
                parsed.append(data)
            except Exception:
                continue
        return web.json_response(parsed)
    except Exception as e:
        return web.Response(status=500, text=f"Error reading stream {stream_to_poll}: {e}")
