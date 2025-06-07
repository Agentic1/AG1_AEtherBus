import asyncio
import os
import json
import argparse
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

try:
    import yaml  # optional for YAML configs
except Exception:  # pragma: no cover - yaml may be absent in minimal env
    yaml = None

def load_llm_config(path: str | None = None) -> dict:
    """Load Azure OpenAI credentials from a file or environment."""
    load_dotenv()
    config: dict = {}
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            if path.endswith(('.yml', '.yaml')) and yaml:
                config = yaml.safe_load(f) or {}
            else:
                config = json.load(f)
    return {
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", config.get("endpoint")),
        "api_key": os.getenv("AZURE_OPENAI_API_KEY", config.get("api_key")),
        "deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT", config.get("deployment")),
        "api_version": os.getenv(
            "AZURE_OPENAI_API_VERSION",
            config.get("api_version", "2024-02-15-preview"),
        ),
    }

keys = StreamKeyBuilder()
REQUEST_STREAM = keys.edge_stream("llm", "requests")

async def handle_llm_request(env: Envelope, redis: Redis, cfg: dict):
    """Process incoming LLM requests and publish responses."""
    prompt = None
    if isinstance(env.content, dict):
        prompt = env.content.get("prompt") or env.content.get("text")
    if not prompt:
        return
    reply_to = env.reply_to or keys.edge_response("llm", env.user_id or env.agent_name)

    result_text = ""
    if openai and cfg.get("endpoint") and cfg.get("api_key") and cfg.get("deployment"):
        openai.api_type = "azure"
        openai.api_base = cfg["endpoint"]
        openai.api_version = cfg["api_version"]
        openai.api_key = cfg["api_key"]
        try:
            resp = await openai.ChatCompletion.acreate(
                engine=cfg["deployment"],
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

async def main(config_path: str | None = None):
    cfg = load_llm_config(config_path)
    redis = Redis.from_url(build_redis_url())
    await start_bus_subscriptions(
        redis=redis,
        patterns=[REQUEST_STREAM],
        group="llm_edge",
        handler=lambda env: handle_llm_request(env, redis, cfg)
    )

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM Edge Handler")
    parser.add_argument(
        "--config",
        help="Path to JSON/YAML file with Azure OpenAI credentials",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main(args.config))
    except KeyboardInterrupt:
        pass
