import asyncio
from dotenv import load_dotenv
load_dotenv()
import json
import os
import aiohttp # For making the HTTP request and handling SSE
from aiohttp_sse_client import client as sse_client # Using a library simplifies SSE handling

from AG1_AetherBus.bus import subscribe, publish_envelope # Your AetherBus components
from AG1_AetherBus.envelope import Envelope
# from AG1_AetherBus.bus import build_redis_url # Assuming this is in your bus.py or accessible
# You'll need your build_redis_url function
import redis.asyncio as aioredis # Direct import if not using your build_redis_url

# --- Configuration ---
SSE_BRIDGE_INBOX = os.getenv("SSE_BRIDGE_INBOX", "sse_bridge.inbox")
# Default AetherBus stream if reply_to is not specified in request for events
DEFAULT_SSE_EVENT_OUTBOX = os.getenv("DEFAULT_SSE_EVENT_OUTBOX", "sse_bridge.events.outbox")

# Your Redis connection setup
def build_redis_url_sse(): # Copied for standalone example, use your existing one
    user = os.getenv("REDIS_USERNAME")
    pwd = os.getenv("REDIS_PASSWORD")
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", 6379))
    if user and pwd:
        return f"redis://{user}:{pwd}@{host}:{port}"
    elif pwd:
        return f"redis://:{pwd}@{host}:{port}"
    else:
        return f"redis://{host}:{port}"

async def listen_to_sse_endpoint(sse_url: str, original_request_env: Envelope, redis_client):
    target_aetherbus_stream = original_request_env.reply_to or DEFAULT_SSE_EVENT_OUTBOX
    correlation_id = original_request_env.correlation_id or original_request_env.envelope_id
    user_id = original_request_env.user_id
    session_code = original_request_env.session_code

    print(f"[SSE_Bridge] Attempting to connect to SSE endpoint: {sse_url} for corr_id: {correlation_id}")

    # Define the timeout configuration
    # total: Total number of seconds for the whole request.
    # connect: Max number of seconds to establish a connection.
    # sock_read: Max number of seconds to read a portion of data.
    # sock_connect: Max number of seconds to connect to a socket.
    # We are interested in a read timeout for the stream.
    # For SSE, 'total' might be indefinite, but we need timeouts for individual reads/keep-alives.
    # The 'read_timeout' in EventSource was likely intended for something like sock_read.
    # Let's set a socket read timeout and a total connection timeout.
    # The aiohttp_sse_client's EventSource doesn't directly expose granular timeout settings in its constructor
    # like read_timeout. It relies on the underlying aiohttp.ClientSession.
    #
    # Looking at aiohttp_sse_client source, it seems 'timeout' kwarg IS supported
    # and it creates a ClientTimeout(total=timeout)
    # The error "unexpected keyword argument 'read_timeout'" means 'read_timeout' specifically is not supported.
    # Let's try the 'timeout' kwarg which maps to ClientTimeout(total=timeout)
    # Or, we can create our own session.

    # Option 1: Using the 'timeout' kwarg if supported by your version of aiohttp-sse-client
    # (This is simpler if it works)
    # sse_connect_timeout = 120 # Total timeout for the connection and stream

    # Option 2: Creating a custom aiohttp.ClientSession for more control (More Robust)
    # This allows setting sock_read_timeout which is closer to what read_timeout implies.
    # sock_read: timeout for how long to wait for a data chunk from the server.
    # For SSE, this is important as events can be infrequent.
    timeout_settings = aiohttp.ClientTimeout(
        total=None, # No total timeout for the entire stream duration
        connect=30,  # Timeout for establishing the connection
        sock_read=120, # Timeout for reading a chunk of data (e.g., one SSE event or keep-alive)
        sock_connect=30
    )
    
    try:
        # Using Option 2: Custom Session
        async with aiohttp.ClientSession(timeout=timeout_settings) as http_session:
            async with sse_client.EventSource(sse_url, session=http_session) as event_source:
                # The 'timeout' argument in EventSource is for the total duration if no session is passed.
                # Since we pass a session with its own timeout config, that should be used.
                async for event in event_source:
                    try:
                        print(f"[SSE_Bridge] Received SSE Event (id={event.id}, event='{event.event}', retry={event.retry}): {event.data[:100]}...")
                        
                        try:
                            event_data_payload = json.loads(event.data)
                        except json.JSONDecodeError:
                            event_data_payload = event.data

                        sse_event_env = Envelope(
                            role="system_event_source", user_id=user_id, agent_name="sse_bridge",
                            session_code=session_code,
                            content={
                                "sse_event_type": event.event, "sse_event_id": event.id,
                                "sse_event_data": event_data_payload, "sse_event_retry": event.retry,
                                "source_sse_url": sse_url
                            },
                            reply_to=None, correlation_id=correlation_id,
                            envelope_type="sse_event_received",
                            meta={"source_envelope_id": original_request_env.envelope_id}
                        )
                        await publish_envelope(redis_client, target_aetherbus_stream, sse_event_env)
                        print(f"[SSE_Bridge] Published SSE event data to {target_aetherbus_stream} for corr_id: {correlation_id}")

                    except Exception as e:
                        print(f"[SSE_Bridge] Error processing individual SSE event for {sse_url}, corr_id: {correlation_id}: {e}")

    except asyncio.TimeoutError: # This would now be triggered by ClientTimeout settings
        print(f"[SSE_Bridge] Timeout based on ClientTimeout settings for SSE endpoint: {sse_url} for corr_id: {correlation_id}")
        error_env = Envelope(
            role="system", content={"error": f"Timeout (ClientTimeout) for SSE: {sse_url}"},
            session_code=session_code, user_id=user_id, agent_name="sse_bridge", envelope_type="error",
            correlation_id=correlation_id, meta={"source_envelope_id": original_request_env.envelope_id}
        )
        await publish_envelope(redis_client, target_aetherbus_stream, error_env)

    except aiohttp.ClientError as e:
        print(f"[SSE_Bridge] ClientError connecting to SSE endpoint: {sse_url} for corr_id: {correlation_id}: {e}")
        error_env = Envelope(
            role="system", content={"error": f"ClientError for SSE {sse_url}: {str(e)}"},
            session_code=session_code, user_id=user_id, agent_name="sse_bridge", envelope_type="error",
            correlation_id=correlation_id, meta={"source_envelope_id": original_request_env.envelope_id}
        )
        await publish_envelope(redis_client, target_aetherbus_stream, error_env)

    except Exception as e:
        print(f"[SSE_Bridge] Unhandled error with SSE endpoint: {sse_url} for corr_id: {correlation_id}: {e}")
        error_env = Envelope(
            role="system", content={"error": f"Unhandled error for SSE {sse_url}: {str(e)}"},
            session_code=session_code, user_id=user_id, agent_name="sse_bridge", envelope_type="error",
            correlation_id=correlation_id, meta={"source_envelope_id": original_request_env.envelope_id}
        )
        await publish_envelope(redis_client, target_aetherbus_stream, error_env)
    finally:
        print(f"[SSE_Bridge] SSE listener for {sse_url} (corr_id: {correlation_id}) concluded.")
        final_env = Envelope(
            role="system", content={"status": f"SSE stream ended for {sse_url}"},
            session_code=session_code, user_id=user_id, agent_name="sse_bridge", envelope_type="sse_stream_status",
            correlation_id=correlation_id, meta={"source_envelope_id": original_request_env.envelope_id}
        )
        await publish_envelope(redis_client, target_aetherbus_stream, final_env)

async def process_sse_request_envelope(env: Envelope, redis_client):
    """
    Handles an incoming request from AetherBus to start listening to an SSE stream.
    """
    print(f"[SSE_Bridge] Received AetherBus request to connect to SSE: {env.envelope_id}")
    content = env.content or {}
    sse_url_to_connect = content.get("sse_url")

    if not sse_url_to_connect:
        print(f"[SSE_Bridge] ERROR: Envelope {env.envelope_id} missing 'sse_url' in content.")
        # Optionally send error back to env.reply_to if it makes sense for a setup request
        if env.reply_to:
            error_env = Envelope(
                role="system", content={"error": "Missing 'sse_url' in request content to SSE bridge"},
                session_code=env.session_code, user_id=env.user_id, agent_name="sse_bridge", envelope_type="error",
                correlation_id=env.correlation_id or env.envelope_id, meta={"source_envelope_id": env.envelope_id}
            )
            await publish_envelope(redis_client, env.reply_to, error_env)
        return

    # Launch the SSE listener in the background (don't await it here)
    # Each SSE connection will run as its own persistent task.
    asyncio.create_task(listen_to_sse_endpoint(sse_url_to_connect, env, redis_client))
    print(f"[SSE_Bridge] Task created to listen to: {sse_url_to_connect} for envelope {env.envelope_id}")


async def main_sse_bridge():
    redis_conn = await aioredis.from_url(build_redis_url_sse())
    print(f"[SSE_Bridge] Subscribing to AetherBus stream: {SSE_BRIDGE_INBOX}")

    async def sse_handler_wrapper(env_data): # Wrapper to ensure Envelope object
        try:
            # Assuming subscribe might pass raw dicts from Redis
            if isinstance(env_data, dict):
                envelope = Envelope.from_dict(env_data)
            elif isinstance(env_data, Envelope):
                envelope = env_data
            else:
                print(f"[SSE_Bridge] Received unexpected data type from bus: {type(env_data)}")
                return
            await process_sse_request_envelope(envelope, redis_conn)
        except Exception as e:
            print(f"[SSE_Bridge] Error in sse_handler_wrapper: {e}")

    # Your AetherBus subscribe function
    # It needs to correctly deserialize the JSON from Redis into an Envelope or dict
    await subscribe(
        redis_conn,
        SSE_BRIDGE_INBOX,
        sse_handler_wrapper, # The callback
        group="sse_bridge_group",
        consumer="sse_bridge_consumer"
    )
    # This might not be reached if subscribe is a forever loop
    # await redis_conn.aclose()

if __name__ == "__main__":
    # You need to install aiohttp and aiohttp_sse_client:
    # pip install aiohttp aiohttp-sse-client
    print("[SSE_Bridge] Starting SSE to AetherBus Bridge...")
    asyncio.run(main_sse_bridge())