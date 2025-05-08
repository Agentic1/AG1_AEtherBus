from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus import publish_envelope
from AG1_AetherBus.envelope import Envelope

key_builder = StreamKeyBuilder()

def resolve_stream_from_envelope(env: Envelope) -> str:
    if env.session_code and env.envelope_type == "message":
        return key_builder.flow_input(env.session_code)  # ✅ FIXED
        #return key_builder.session_stream(env.session_code)
    elif env.agent_name:
        return key_builder.agent_inbox(env.agent_name)
    elif env.envelope_type == "register" and env.headers.get("platform"):
        return key_builder.edge_register(env.headers["platform"])
    elif env.headers.get("flow") and env.envelope_type == "flow":
        return key_builder.flow_input(env.headers["flow"])
    raise ValueError("Cannot resolve stream from envelope")

async def publish_to_resolved_stream(redis, env: Envelope):
    stream = resolve_stream_from_envelope(env)
    print(f'[utils][Publish_toresolved_stream] {stream}')
    await publish_envelope(redis, stream, env)