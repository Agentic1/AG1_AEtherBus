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
import time
from openai import AzureOpenAI


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
client = None
REQUEST_STREAM = keys.edge_stream("llm", "requests")

def print_timing(start_time: float, step_name: str):
    """Helper to print time taken for each step"""
    elapsed = (time.time() - start_time) * 1000  # Convert to milliseconds
    print(f"[TIMING] {step_name} took {elapsed:.2f}ms")


async def handle_llm_request(env: Envelope, redis: Redis, cfg: dict):
    """Process incoming LLM requests and publish responses."""
    total_start = time.time()
    
    print(f"\n=== LLM Request Received ===")
    print(f"Envelope ID: {getattr(env, 'envelope_id', 'N/A')}")
    print(f"From: {env.agent_name}, User: {getattr(env, 'user_id', 'N/A')}")
    print(f"Target: {env.target}, Reply To: {env.reply_to}")
    print(f"Correlation ID: {env.correlation_id}")
    print(f"Content Type: {type(env.content)}") 
    
    # Initialize client if needed
    client_start = time.time()
    global client
    if client is None:
        print("Initializing Azure OpenAI client...")
        client = AzureOpenAI(
            api_key=cfg["api_key"],
            api_version=cfg.get("api_version", "2024-05-01-preview"),
            azure_endpoint=cfg["endpoint"]
        )
    print_timing(client_start, "Client initialization")
    
    # Get prompt from request
    prompt = None
    if isinstance(env.content, dict):
        prompt = env.content.get("prompt") or env.content.get("text")
        print(f"Prompt: {str(prompt)[:100]}{'...' if len(str(prompt)) > 100 else ''}")
    if not prompt:
        error_msg = "Error: No prompt found in request"
        print(error_msg)
        return
        
    reply_to = env.reply_to or keys.edge_response("llm", getattr(env, 'user_id', env.agent_name))
    print(f"Will reply to: {reply_to}")

    # Make API call
    api_start = time.time()
    try:
        print("Sending request to Azure OpenAI...")
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=cfg["deployment"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=env.content.get("max_tokens", 500),
            temperature=env.content.get("temperature", 0.7),
            top_p=env.content.get("top_p", 0.95)
        )
        print_timing(api_start, "Azure OpenAI API call")
        
        # Process response
        process_start = time.time()
        result_text = response.choices[0].message.content
        print(f"Response length: {len(result_text)} characters")
        print(f"Response : {result_text[:100]}{'...' if len(result_text) > 100 else ''} characters")
        print_timing(process_start, "Response processing")
        
        # Send response
        send_start = time.time()
        response_env = Envelope(
            role="llm",
            content={"text": result_text},
            agent_name="llm_edge",
            target=env.agent_name,
            reply_to=reply_to,
            correlation_id=env.correlation_id
        )
        
        await publish_envelope(redis, reply_to, response_env)
        print_timing(send_start, "Sending response")
        print("Response sent successfully")
        
    except Exception as e:
        error_msg = f"Error calling Azure OpenAI: {str(e)}"
        print(error_msg)
        error_env = Envelope(
            role="llm",
            content={"error": error_msg},
            agent_name="llm_edge",
            target=env.agent_name,
            reply_to=reply_to,
            correlation_id=env.correlation_id
        )
        await publish_envelope(redis, reply_to, error_env)
    finally:
        print_timing(total_start, "Total request processing")
        print("="*50 + "\n")

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
