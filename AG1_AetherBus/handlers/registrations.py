from AG1_AetherBus.envelope import Envelope
from redis.asyncio import Redis

# Registry of agents able to handle AetherDeck traffic
# {pattern: {"agent_name": str, "agent_inbox_stream": str, "timestamp": str}}
aetherdeck_registered_agents = {}

async def handle_aetherdeck_registration_envelope(env: Envelope, redis_client: Redis):
    """Process agent registration envelopes for the AetherDeck channel."""
    if env.envelope_type == "register" and env.content.get("channel_type") == "aetherdeck":
        agent_name = env.agent_name
        pattern = env.content.get("aetherdeck_user_id_pattern")
        agent_inbox_stream = env.content.get("agent_inbox_stream")
        timestamp = env.timestamp

        if agent_name and pattern and agent_inbox_stream:
            aetherdeck_registered_agents[pattern] = {
                "agent_name": agent_name,
                "agent_inbox_stream": agent_inbox_stream,
                "timestamp": timestamp,
            }
            print(
                f"[AETHERDECK_HANDLER][REGISTER] Registered agent '{agent_name}' for AetherDeck user pattern '{pattern}'. Will forward events to: {agent_inbox_stream}"
            )
        else:
            print(f"[AETHERDECK_HANDLER][REGISTER][WARN] Invalid AetherDeck registration envelope: {env}")
    else:
        print(
            f"[AETHERDECK_HANDLER][REGISTER][INFO] Ignoring non-AetherDeck registration or non-register envelope: {env.envelope_type}, channel_type: {env.content.get('channel_type')}"
        )
