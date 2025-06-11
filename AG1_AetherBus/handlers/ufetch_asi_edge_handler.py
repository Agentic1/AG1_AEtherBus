#!/usr/bin/env python3
"""
uFetch Edge Handler - Example connector for Fetch.ai agents

This bridge listens on AetherBus for envelopes addressed to the uFetch
edge. Each request is forwarded to a Fetch.ai agent over HTTP and the
result is published back to the requester via AetherBus.

The design mirrors ``ata_example_bridge.py`` but targets Fetch network
agents instead of A2A services.
"""

import asyncio
import json
from typing import Dict, Any

import aiohttp
import redis.asyncio as aioredis
from dotenv import load_dotenv

# AetherBus utilities
from AG1_AetherBus.bus import build_redis_url, publish_envelope
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus_adapterV2 import BusAdapterV2

load_dotenv()

# --- Constants -------------------------------------------------------------
#export ASI_KEY="sk_8f5163427ac34c73af224f4de6a72e3a80da63f778e2447cbcd439f2318e8201
keys = StreamKeyBuilder()

# Request channel used by agents to reach the Fetch network
UFETCH_INBOX = "AG1:ufetch:agent:edge:inbox"

# Channel for agents to register their Fetch endpoints (optional)
UFETCH_REGISTER = keys.edge_register("ufetch")

# Registered agents {name: {endpoint: str, auth_key: str}}
registered_agents: Dict[str, Dict[str, Any]] = {}


# --- Helper functions ------------------------------------------------------
async def call_fetch_endpoint(endpoint: str, payload: Dict[str, Any], auth_key: str | None) -> Dict[str, Any]:
    """POST the payload to the remote Fetch agent and return the JSON result."""
    print(f"[UFETCH_BRIDGE][CALL_ENDPOINT] Attempting POST to: {endpoint}") # DEBUG
    print(f"[UFETCH_BRIDGE][CALL_ENDPOINT] With Auth: {'Yes' if auth_key else 'No'}, Payload: {str(payload)[:200]}") # DEBUG
    headers = {"Content-Type": "application/json"}
    if auth_key:
        headers["Authorization"] = f"Bearer {auth_key}"

    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=payload, headers=headers) as resp:
            print(f"[UFETCH_BRIDGE][CALL_ENDPOINT] HTTP Status: {resp.status}") # DEBUG
            resp.raise_for_status()
            return await resp.json()


# --- Handlers --------------------------------------------------------------
async def handle_registration(env: Envelope, redis_client) -> None:
    """Register a Fetch agent and its endpoint."""
    agent_name = env.agent_name
    content = env.content or {}
    endpoint = content.get("fetch_endpoint")
    auth_key = content.get("auth_key")

    if not agent_name or not endpoint:
        return

    registered_agents[agent_name] = {"endpoint": endpoint, "auth_key": auth_key}

    confirm = Envelope(
        role="system",
        envelope_type="registration_confirmation",
        agent_name="ufetch_edge",
        content={"status": "registered"},
        correlation_id=env.correlation_id,
    )
    await publish_envelope(redis_client, keys.edge_response("ufetch", agent_name), confirm)


async def handle_ufetch_request(env: Envelope, redis_client) -> None:
    """Forward an envelope's payload to the target Fetch agent."""
    print(f"[UFETCH_BRIDGE][HANDLE_REQUEST] Received request. Envelope ID: {env.envelope_id}, ReplyTo: {env.reply_to}")
    print(f"[UFETCH_BRIDGE][HANDLE_REQUEST] Content: {env.content}")
    content = env.content or {}
    target_agent = content.get("target_fetch_agent")
    endpoint = content.get("fetch_endpoint")
    payload = content.get("payload", {})
    auth_key = content.get("auth_key")

    if not endpoint and target_agent in registered_agents:
        reg = registered_agents[target_agent]
        endpoint = reg.get("endpoint")
        auth_key = auth_key or reg.get("auth_key")

    if not endpoint:
        error_env = Envelope(
            role="system",
            envelope_type="error",
            agent_name="ufetch_edge",
            correlation_id=env.correlation_id,
            content={"error": "missing fetch_endpoint"},
        )
        print(f"[UFETCH_BRIDGE][DEBUG_REPLY_PUBLISH] Publishing to stream='{env.reply_to}', data='{str(response_env.to_dict())[:200]}...'")
        await publish_envelope(redis_client, env.reply_to, error_env)
        return

    try:
        result = await call_fetch_endpoint(endpoint, payload, auth_key)
        print(f"[UFETCH_BRIDGE][HANDLE_REQUEST] Successfully called Fetch endpoint. Result: {str(result)[:100]}")
        response_env = Envelope(
            role="agent",
            envelope_type="ufetch_response",
            agent_name="ufetch_edge",
            correlation_id=env.correlation_id,
            content={"result": result},
        )
    except Exception as exc:  # HTTP or decoding errors
        print(f"[UFETCH_BRIDGE][HANDLE_REQUEST][ERROR] Exception calling Fetch endpoint: {exc}")
        response_env = Envelope(
            role="system",
            envelope_type="error",
            agent_name="ufetch_edge",
            correlation_id=env.correlation_id,
            content={"error": str(exc)},
        )
    print(f"[UFETCH_BRIDGE][HANDLE_REQUEST] Publishing response to: {env.reply_to}. Response type: {response_env.envelope_type}")
    await publish_envelope(redis_client, env.reply_to, response_env)


# --- Main -----------------------------------------------------------------
async def main() -> None:
    redis_client = aioredis.from_url(build_redis_url())

    async def handler_with_redis(env: Envelope) -> None:
        await handle_ufetch_request(env, redis_client)

    adapter = BusAdapterV2(
        agent_id="ufetch_edge",
        redis_client=redis_client,
        core_handler=handler_with_redis,
        patterns=[UFETCH_INBOX],
        group="ufetch_edge_group",
    )

    await adapter.add_subscription(UFETCH_REGISTER, lambda env: handle_registration(env, redis_client))
    await adapter.start()

    print(f"[ufetch_edge] Listening on {UFETCH_INBOX}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())