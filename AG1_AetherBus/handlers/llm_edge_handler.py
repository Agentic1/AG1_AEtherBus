import asyncio
import os
from dotenv import load_dotenv
from redis.asyncio import Redis

from AG1_AetherBus.bus import build_redis_url, publish_envelope
from AG1_AetherBus.agent_bus_minimal import start_bus_subscriptions
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder

try:
    import openai
except ImportError:  # pragma: no cover - openai optional for tests
    openai = None

load_dotenv()

# Azure OpenAI configuration
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

keys = StreamKeyBuilder()
REQUEST_STREAM = keys.edge_stream("llm", "requests")

async def handle_llm_request(env: Envelope, redis: Redis):
    """Process incoming LLM requests and publish responses."""
    prompt = None
    if isinstance(env.content, dict):
        prompt = env.content.get("prompt") or env.content.get("text")
    if not prompt:
        return
    reply_to = env.reply_to or keys.edge_response("llm", env.user_id or env.agent_name)

    result_text = ""
    if openai and AZURE_ENDPOINT and AZURE_API_KEY and AZURE_DEPLOYMENT:
        openai.api_type = "azure"
        openai.api_base = AZURE_ENDPOINT
        openai.api_version = AZURE_API_VERSION
        openai.api_key = AZURE_API_KEY
        try:
            resp = await openai.ChatCompletion.acreate(
                engine=AZURE_DEPLOYMENT,
                messages=[{"role": "user", "content": prompt}]
            )
            result_text = resp.choices[0].message.content
        except Exception as e:  # pragma: no cover - network errors
            result_text = f"LLM error: {e}"
    else:
        result_text = "LLM backend not configured"

    response_env = Envelope(
        role="llm",
        content={"text": result_text},
        user_id=env.user_id,
        agent_name="llm_edge",
        correlation_id=env.correlation_id,
        envelope_type="message",
    )
    await publish_envelope(redis, reply_to, response_env)

async def main():
    redis = Redis.from_url(build_redis_url())
    await start_bus_subscriptions(
        redis=redis,
        patterns=[REQUEST_STREAM],
        group="llm_edge",
        handler=lambda env: handle_llm_request(env, redis)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
