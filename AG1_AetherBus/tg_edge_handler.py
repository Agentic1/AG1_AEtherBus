import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties

from redis.asyncio import Redis
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus import publish_envelope, subscribe, build_redis_url
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
import uuid
import contextlib

# AG1_muse_bot
# AG1.muse
# 8194289566:AAFipXxxkJCY-ev8fGE19ETh0FzAeqlZcVk

# --- Config ---
TELEGRAM_BOT_TOKEN = "8194289566:AAFipXxxkJCY-ev8fGE19ETh0FzAeqlZcVk"
AGENT_NAME = "muse"
TG_HANDLE = "@AG1_muse_bot"

# --- Multi-Agent Broadcast Flag ---
# Set to True to enable broadcasting agent replies to all other agents
ENABLE_AGENT_BROADCAST = False

# --- Multi-Agent Broadcast Function ---

def register_bot_handlers(dp, bot, tg_handle, agent_name, redis, keys, registered_agents):
    """Register all handlers for a bot/dispatcher."""
    @dp.message()
    async def handle_message(message):
        # Determine if message is from a group or private chat
        if message.chat.type in ("group", "supergroup"):
            reply_chat_id = message.chat.id  # group chat
        else:
            reply_chat_id = message.from_user.id  # private chat
        user_id = str(message.from_user.id)
        text = message.text
        print(f"[TG_EDGE][TG][DEBUG] [{tg_handle}] Received message from user {user_id} in chat {message.chat.id} (type={message.chat.type}): {str(text)[:40]}{'...' if text and len(str(text)) > 40 else ''}")
        # --- /stop command ---
        if text and text.strip().lower() == "/stop":
            await message.answer("Bot stopped. To restart, re-register the agent.")
            return
        agent_info = registered_agents.get(tg_handle)
        if agent_info:
            agent_name = agent_info["agent_name"]
            correlation_id = str(uuid.uuid4())
            reply_to_stream = keys.edge_response("tg", tg_handle)
            inbox_stream = keys.agent_inbox(agent_name)
            envelope = Envelope(
                role="user",
                user_id=user_id,
                agent_name=agent_name,
                content={"text": text},
                reply_to=reply_to_stream,
                envelope_type="message",
                correlation_id=correlation_id,
                meta={"reply_chat_id": reply_chat_id},
            )
            env_preview = str(envelope.to_dict())
            if len(env_preview) > 40:
                env_preview = env_preview[:40] + '...'
            print(f"[TG_EDGE][TG][DEBUG] [{tg_handle}] Publishing Envelope to agent inbox: {inbox_stream}\n{env_preview}")
            try:
                await publish_envelope(redis, inbox_stream, envelope)
                print(f"[TG_EDGE][TG][DEBUG] [{tg_handle}] Envelope published to {inbox_stream}")
            except Exception as e:
                print(f"[TG_EDGE][TG][ERROR] [{tg_handle}] Failed to publish Envelope: {e}")
        else:
            print(f"[TG_EDGE][TG][WARN] [{tg_handle}] No agent info found for this handle!")

async def broadcast_envelope_to_other_agents(envelope, sender_agent_name, agent_registry, redis, throttle_seconds=3):
    """
    Broadcasts the given envelope to all registered agents except the sender.
    Waits throttle_seconds between each send to avoid flooding.
    Prevents rebroadcast loops by skipping envelopes that are already group messages or agent-generated.
    """
    # REMOVED FOR TDLib SIMPLIFICATION: agent/bot relay/loop logic
    # Guard: Do not rebroadcast agent-generated or group_message envelopes
    # if getattr(envelope, 'envelope_type', None) == "group_message" or (getattr(envelope, 'meta', {}) or {}).get("agent_generated"):
    #     print("[broadcast] Skipping rebroadcast of agent-generated or group_message envelope.")
    #     return
    for tg_handle, info in agent_registry.items():
        agent_name = info.get("agent_name")
        # REMOVED FOR TDLib SIMPLIFICATION: skip sender agent
        # if agent_name and agent_name != sender_agent_name:
        #     # build and send new_env
        if agent_name:
            # build and send new_env
            meta = dict(envelope.meta or {})
            #PAtch NO LOOP XXXX
            #meta["agent_generated"] = True
            meta["origin_agent"] = sender_agent_name
            new_env = Envelope(
                envelope_type="group_message",
                content=envelope.content,
                meta=meta,
                user_id=envelope.user_id,
                agent_name=envelope.agent_name,
                # Above change for loop hackagent_name=agent_name,
                correlation_id=envelope.correlation_id,
                reply_to=envelope.reply_to,
                role=envelope.role,
            )
            inbox = keys.agent_inbox(agent_name)
            print(f"tg_sender][broadcast to other agents] Relaying agent reply from {sender_agent_name} to {agent_name} inbox: {inbox}")
            await publish_envelope(redis, inbox, new_env)
            await asyncio.sleep(throttle_seconds)

# --- Redis Setup ---
redis = Redis.from_url(build_redis_url())
keys = StreamKeyBuilder()

# --- Telegram Bot Setup ---
session = AiohttpSession()
bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

import asyncio
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions

# --- Agent Registry ---
registered_agents = {}

# --- Running Bot Instances ---
running_bots = {}  # {tg_handle: bot_instance}

# --- Reporter Task ---
import datetime
async def reporter_task(agent_registry, running_bots, interval=15):
    while True:
        print("\n[REPORTER] --- AGENT REGISTRY STATUS ---")
        if not agent_registry:
            print("[REPORTER] No agents currently registered.")
        else:
            for tg_handle, info in agent_registry.items():
                agent_name = info.get("agent_name")
                key = info.get("key")
                timestamp = info.get("timestamp")
                print(f"  - {agent_name} ({tg_handle}) | Key: {str(key)[:6]}... | Registered: {timestamp}")
        print("[REPORTER] --- RUNNING BOT INSTANCES ---")
        if not running_bots:
            print("[REPORTER] No Telegram bot listeners running.")
        else:
            for tg_handle in running_bots:
                print(f"  - Bot for {tg_handle} is running.")
        print(f"[REPORTER] Time: {datetime.datetime.now().isoformat()}")
        print("[REPORTER] -------------------------------\n")
        await asyncio.sleep(interval)

async def handle_agent_reply(env, bot, tg_handle, registered_agents, redis):
    if getattr(env, "envelope_type", None) == "message":
        reply_chat_id = env.meta.get("reply_chat_id") if env.meta else None
        text = env.content.get("text") if env.content else None
        if reply_chat_id and text:
            try:
                await bot.send_message(reply_chat_id, text)
                trunc_text = str(text)[:40] + ('...' if text and len(str(text)) > 40 else '')
                print(f"[TG_EDGE][BOT][DEBUG] [{tg_handle}] Sent agent reply to user {env.user_id} in chat {reply_chat_id}: {trunc_text}")
            except Exception as e:
                print(f"[TG_EDGE][BOT][ERROR] [{tg_handle}] Failed to send agent reply: {e}")
        else:
            print(f"[TG_EDGE][BUS][WARN] [{tg_handle}] Missing reply_chat_id or text in agent reply: {env}")
    elif getattr(env, "envelope_type", None) == "register":
        agent_name = getattr(env, "agent_name", None)
        content = getattr(env, "content", {})
        tg_handle = content.get("tg_handle")
        key = content.get("key")
        if agent_name and tg_handle and key:
            registered_agents[tg_handle] = {
                "agent_name": agent_name,
                "key": key,
                "timestamp": getattr(env, "timestamp", None)
            }
            print(f"[TG_EDGE][REGISTER] Registered agent: {agent_name} as {tg_handle}")
            if tg_handle not in running_bots:
                dp = Dispatcher()
                register_bot_handlers(dp, bot, tg_handle, agent_name, redis, keys, registered_agents)
                running_bots[tg_handle] = bot
                asyncio.create_task(dp.start_polling(bot))
                print(f"[TG_EDGE][BOT][DEBUG] Started bot for {tg_handle}")
                # Subscribe to agent reply stream for this handle
                reply_stream = keys.edge_response("tg", tg_handle)
                group = f"tg_edge_{tg_handle}"
                import functools
                asyncio.create_task(
                    subscribe(
                        redis,
                        reply_stream,
                        functools.partial(handle_agent_reply, bot=bot, tg_handle=tg_handle, registered_agents=registered_agents, redis=redis),
                        group=group
                    )
                )
            else:
                print(f"[TG_EDGE][BOT][DEBUG] Bot for {tg_handle} is already running.")
        else:
            print(f"[TG_EDGE][REGISTER][WARN] Invalid registration envelope: {env}")

async def start_polling(dp, bot, tg_handle):
    asyncio.create_task(dp.start_polling(bot))

async def handle_register_envelope(env, redis):
    if getattr(env, "envelope_type", None) == "register":
        agent_name = getattr(env, "agent_name", None)
        content = getattr(env, "content", {})
        tg_handle = content.get("tg_handle")
        key = content.get("key")
        if agent_name and tg_handle and key:
            registered_agents[tg_handle] = {
                "agent_name": agent_name,
                "key": key,
                "timestamp": getattr(env, "timestamp", None)
            }
            print(f"[TG_EDGE][REGISTER] Registered agent: {agent_name} as {tg_handle}")
            if tg_handle not in running_bots:
                bot_instance = Bot(
                    token=key,  # Use the agent's unique bot token!
                    session=session,
                    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
                )
                dp = Dispatcher()
                register_bot_handlers(dp, bot_instance, tg_handle, agent_name, redis, keys, registered_agents)
                running_bots[tg_handle] = bot_instance
                if 'running_dispatchers' not in globals():
                    global running_dispatchers
                    running_dispatchers = {}
                running_dispatchers[tg_handle] = dp
                asyncio.create_task(dp.start_polling(bot_instance))
                print(f"[TG_EDGE][BOT][DEBUG] Started bot for {tg_handle}")
                # Subscribe to agent reply stream for this handle
                reply_stream = keys.edge_response("tg", tg_handle)
                group = f"tg_edge_{tg_handle}"
                import functools
                asyncio.create_task(
                    subscribe(
                        redis,
                        reply_stream,
                        functools.partial(handle_agent_reply, bot=bot_instance, tg_handle=tg_handle, registered_agents=registered_agents, redis=redis),
                        group=group
                    )
                )
            else:
                print(f"[TG_EDGE][BOT][DEBUG] Bot for {tg_handle} is already running.")
        else:
            print(f"[TG_EDGE][REGISTER][WARN] Invalid registration envelope: {env}")

if __name__ == "__main__":
    import asyncio
    import sys

    async def handler_with_redis(env):
        await handle_register_envelope(env, redis)

    async def main():
        print("[TG_EDGE] Running in MULTI-BOT MODE: accepting agent registrations and launching Telegram bots dynamically.")
        patterns = ["AG1:tg:register"]
        # Start the reporter task for visibility (optional)
        asyncio.create_task(reporter_task(registered_agents, running_bots, interval=15))
        # Listen for new agent registrations and handle them (which will launch bots and reply subscriptions as needed)
        await start_bus_subscriptions(
            redis=redis,
            patterns=patterns,
            group="tg_edge_handler",
            handler=handler_with_redis
        )
        # Keep the process alive (should never exit)
        await asyncio.Event().wait()

    asyncio.run(main())

