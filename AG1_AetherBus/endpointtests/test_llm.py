# test_llm.py
import asyncio
import uuid
from AG1_AetherBus.bus import publish_envelope
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

# Initialize key builder
keys = StreamKeyBuilder()

# Define the request and response stream keys
REQUEST_STREAM = "AG1:edge:llm:requests:stream"  # Fixed stream name to match handler
RESPONSE_STREAM = "llm_responses"  # This should match exactly what the handler uses

# Event to signal when we get a response
response_received = asyncio.Event()
response_data = None

async def handle_response(env: Envelope):
    global response_data, response_received, sub_task
    print(f"Received response in test: {env.content}")
    response_data = env.content
    response_received.set()
    
    # Cancel the subscription task since we got our response
    if 'sub_task' in globals() and sub_task and not sub_task.done():
        sub_task.cancel()
    
    return True

async def test_llm():
    # Generate a unique correlation ID
    correlation_id = str(uuid.uuid4())
    
    # Create a test prompt
    test_env = Envelope(
        role="user",
        content={
            "prompt": "Whats trumps issue with musk?",
            "max_tokens": 50
        },
        agent_name="test_llm",
        target="llm_edge",
        reply_to=RESPONSE_STREAM,  # Make sure this matches what the handler expects
        correlation_id=correlation_id
    )
    
    # Start subscription to responses
    redis = None
    try:
        from AG1_AetherBus.bus import build_redis_url
        from redis.asyncio import Redis
        redis = Redis.from_url(build_redis_url())

        cleanup_redis = await Redis.from_url(build_redis_url())
        try:
            # Delete the entire stream
            await cleanup_redis.delete(RESPONSE_STREAM)
        except Exception as e:
            print(f"Warning: Could not clean up stream: {e}")
        finally:
            await cleanup_redis.aclose()
        
        # Debug: Print the response stream key
        print(f"Listening on response stream: {RESPONSE_STREAM}")
        
        # Start subscription to the response stream
        sub_task = asyncio.create_task(
            start_bus_subscriptions(
                redis=redis,
                patterns=[RESPONSE_STREAM],  # Listen on the response stream
                group="test_client",
                handler=handle_response
            )
        )
        
        # Give it a moment to subscribe
        await asyncio.sleep(1)
        
        # Send the request
        await publish_envelope(redis, REQUEST_STREAM, test_env)
        print(f"Sent request with correlation_id: {correlation_id}")
        
        # Wait for response with timeout
        try:
            await asyncio.wait_for(response_received.wait(), timeout=30.0)
            if response_data:
                print("Response received:", response_data)
            else:
                print("Received empty response")
        except asyncio.TimeoutError:
            print("Timed out waiting for response")
            # Debug: Check what's in the stream
            response = await redis.xread({RESPONSE_STREAM: '0-0'}, count=10)
            print(f"Debug - Raw response from stream: {response}")
        
    finally:
        # Cleanup
        if 'sub_task' in locals():
            sub_task.cancel()
            try:
                await sub_task
            except asyncio.CancelledError:
                pass
        
        if redis:
            # Close the Redis connection gracefully
            await redis.aclose()
            # Add a small delay to allow any pending operations to complete
            await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(test_llm())