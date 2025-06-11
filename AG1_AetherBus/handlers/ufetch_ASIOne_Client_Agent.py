# --- START OF FILE ASIOne_Client_Agent.py ---
"""
XADD AG1:agent:ASIOneClientAgent:inbox * data '{"role":"user_request","agent_name":"redis_cli_test","content":{"prompt":"What is the Fetch.ai Almanac service?"},"reply_to":"AG1:temp:my_asi_test_reply","correlation_id":"my_cli_corr_001","user_id":"cli_user_123"}'
"""
import asyncio
import json
import os
import uuid
import sys
from typing import Dict, Any
import logging
import redis.asyncio as aioredis

# Ensure paths are set up to find AG1_AetherBus
# This might need adjustment based on your project structure
try:
    ROOT_DIR_GUESS = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))
    if ROOT_DIR_GUESS not in sys.path:
        sys.path.insert(0, ROOT_DIR_GUESS)
    print(f"[ASIOneClient][INIT] Added to sys.path: {ROOT_DIR_GUESS}")
except Exception as e_path:
    print(f"[ASIOneClient][INIT][PATH_ERROR] Error setting up sys.path: {e_path}")


from redis.asyncio import Redis
from AG1_AetherBus.bus_adapterV2 import BusAdapterV2
from AG1_AetherBus.envelope import Envelope
from AG1_AetherBus.keys import StreamKeyBuilder
from AG1_AetherBus.bus import build_redis_url, publish_envelope
from AG1_AetherBus.rpc import bus_rpc_call, bus_rpc_envelope # Using bus_rpc_call for simplicity

# Configuration 
AGENT_NAME = "ASIOneClientAgent"
AGENT_INBOX = StreamKeyBuilder().agent_inbox(AGENT_NAME)
UFETCH_BRIDGE_INBOX = "AG1:ufetch:agent:edge:inbox" # Inbox of ufetch_example_bridge.py

# ASI:One Configuration (Ideally from .env or a config file)
ASI_ONE_API_ENDPOINT = "https://api.asi1.ai/v1/chat/completions"
ASI_ONE_API_KEY = os.getenv("ASI_KEY", "sk_8f5163427ac34c73af224f4de6a72e3a80da63f778e2447cbcd439f2318e8201") # Ensure ASI_KEY is in your env
ASI_ONE_MODEL = "asi1-mini"

#export ASI_KEY="sk_8f5163427ac34c73af224f4de6a72e3a80da63f778e2447cbcd439f2318e8201
if ASI_ONE_API_KEY == "place_holder":
    print(f"[{AGENT_NAME}][WARN] ASI_KEY environment variable not set or using placeholder!")

class ASIOneClientAgent:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client
        self.name = AGENT_NAME
        self.adapter = BusAdapterV2(
            agent_id=self.name,
            core_handler=self.handle_bus_request,
            redis_client=self.redis,
            patterns=[AGENT_INBOX],
            group=self.name
        )
        print(f"[{self.name}] Initialized. Listening on {AGENT_INBOX}")

    async def start(self):
        await self.adapter.start()
        print(f"[{self.name}] Adapter started.")

    async def handle_bus_request(self, request_env: Envelope, bus_conn: Redis):
        print(f"--- [ASIOneClient] TOP OF handle_bus_request ---") # DEBUG
        print(f"[{self.name}] Received request: {request_env.envelope_id} from {request_env.agent_name} on {request_env.reply_to}")
        print(f"[{self.name}] Incoming content: {request_env.content}") # DEBUG
        prompt_text = (request_env.content or {}).get("prompt")
        query_text = (request_env.content or {}).get("text")
        temperature = (request_env.content or {}).get("temperature", 0.7) # Default temperature

        print(f'[ASIOne]--> query text {query_text} prompttext {prompt_text}  ----<')
        if not prompt_text:
            prompt_text= query_text
            error_content = {"text": "Error: 'prompt' missing in request content. sing query text"}
            print(f"[{self.name}][Warning!!!!] {error_content['text']}")
            """if request_env.reply_to:
                error_env = Envelope(role="error", agent_name=self.name, content=error_content,
                                     correlation_id=request_env.correlation_id, user_id=request_env.user_id)
                await publish_envelope(bus_conn, request_env.reply_to, error_env)"""
            #return

        # Construct payload for ASI:One (via uFetch bridge)
        asi_one_payload = {
            "model": ASI_ONE_MODEL,
            "messages": [{"role": "user", "content": prompt_text}],
            "temperature": temperature
        }

        # Construct envelope for uFetch_Edge_Bridge
        ufetch_request_content = {
            "fetch_endpoint": ASI_ONE_API_ENDPOINT,
            "auth_key": ASI_ONE_API_KEY, # uFetch bridge expects "auth_key"
            "payload": asi_one_payload
        }
        
        # We need a unique reply_to for the RPC call to the bridge
        # bus_rpc_call handles creating a temporary reply stream.
        rpc_request_env = Envelope(
            role="agent_request",
            agent_name=self.name,
            content=ufetch_request_content,
            user_id=request_env.user_id, # Pass along original user context
            correlation_id=str(uuid.uuid4()), # New correlation for this sub-request
            # reply_to for bus_rpc_call is handled by the function itself
        )
        print(f"[{self.name}] Preparing to call uFetch bridge. Request to bridge: {rpc_request_env.to_dict()}") # DEBUG: 
        print(f"[{self.name}] Sending request to uFetch bridge ({UFETCH_BRIDGE_INBOX}) for prompt: '{prompt_text}...'")

        final_response_text = "Error: No response or failed call to ASI:One via uFetch bridge."
        try:
            # Rename variable for clarity, as bus_rpc_envelope returns Envelope or Dict or None
            bridge_response_object = await bus_rpc_envelope(
                self.redis,
                UFETCH_BRIDGE_INBOX,
                rpc_request_env,
                timeout=25.0 
            )

            print(f"[{self.name}] Received from bus_rpc_envelope: {type(bridge_response_object)} | Content (if any): {str(getattr(bridge_response_object, 'content', bridge_response_object))[:200]}")

            if isinstance(bridge_response_object, Envelope):
                # Successfully got an Envelope object from the bridge
                bridge_content = bridge_response_object.content or {} # Ensure content is a dict

                # Check if the bridge's envelope itself indicates an error
                if bridge_response_object.envelope_type == "error" or bridge_content.get("error"):
                    final_response_text = f"Error from uFetch bridge: {bridge_content.get('error', 'Unknown error from bridge envelope')}"
                    print(f"[{self.name}][ERROR_FROM_BRIDGE] {final_response_text}")
                else:
                    # Bridge response is not an error, expect ASI:One result in content.result
                    asi_one_result = bridge_content.get("result", {})
                    if not asi_one_result: # Check if "result" key exists and is not empty
                        final_response_text = "ASI:One response missing 'result' field from bridge."
                        print(f"[{self.name}][WARN] Bridge response content missing 'result': {bridge_content}")
                    else:
                        llm_reply = (asi_one_result.get("choices", [{}])[0].get("message", {}).get("content", "").strip())
                        if llm_reply:
                            final_response_text = llm_reply
                        else:
                            final_response_text = "ASI:One replied, but content was not in expected format."
                            print(f"[{self.name}][WARN] Unexpected ASI:One result structure in bridge response: {asi_one_result}")
            
            elif isinstance(bridge_response_object, dict) and "error" in bridge_response_object:
                # bus_rpc_envelope itself returned an error dictionary (e.g., timeout from bus_rpc_call)
                final_response_text = f"RPC Error: {bridge_response_object['error']}"
                print(f"[{self.name}][ERROR_RPC_LAYER] {final_response_text}")
            
            else: # None or other unexpected type from bus_rpc_envelope
                final_response_text = f"Error: No valid response or unexpected type ({type(bridge_response_object)}) from bus_rpc_envelope."
                print(f"[{self.name}][WARN_UNEXPECTED_RPC_RETURN] {final_response_text}")

        except asyncio.TimeoutError: 
            final_response_text = "Error: Timeout waiting for response from uFetch bridge / ASI:One (asyncio layer)."
            print(f"[{self.name}][ERROR_TIMEOUT_ASYNCIO] {final_response_text}")
        except Exception as e: 
            final_response_text = f"Error processing ASI:One request (outer try): {str(e)}"
            print(f"[{self.name}][ERROR_GENERAL_EXCEPTION_OUTER] {final_response_text}")
            import traceback
            traceback.print_exc()

        # Send the final LLM text (or error) back to the original requester (e.g., your XADD)
        if request_env.reply_to:
            reply_content = {"text": final_response_text}
            response_env_to_initiator = Envelope(
                role="agent_response",
                agent_name=self.name,
                content=reply_content,
                user_id=request_env.user_id,
                correlation_id=request_env.correlation_id, 
                meta=request_env.meta, 
                reply_to=request_env.reply_to ,

                session_code=request_env.meta.get("session_id"),
               
            )
            await publish_envelope(bus_conn, request_env.reply_to, response_env_to_initiator)
            print(f"[{self.name}] Sent final response to {request_env.reply_to}: {final_response_text[:100]}...")
        else:
            print(f"[{self.name}][WARN] Original request had no reply_to. Cannot send response: {final_response_text[:100]}...")
        print(f"--- [ASIOneClient] BOTTOM OF handle_bus_request ---")


async def main():
    print(f"[{AGENT_NAME}] Starting...")
    redis_url =  build_redis_url() #build_aetherbus_redis_url()
    if not redis_url or "None" in redis_url or "none" in str(redis_url).lower():
        print(f"[{AGENT_NAME}][WARNING] AetherBus Redis URL from ENV was invalid ('{redis_url}'), using default.")
        redis_url = "redis://localhost:6379/1" # Default AetherBus Redis DB 1
    
    redis_client = await aioredis.Redis.from_url(redis_url, decode_responses=True)
    try:
        await redis_client.ping()
        print(f"[{AGENT_NAME}] Connected to AetherBus Redis at {redis_url}")
    except Exception as e:
        print(f"[{AGENT_NAME}][ERROR] Failed to connect to AetherBus Redis: {e}")
        return

    agent = ASIOneClientAgent(redis_client)
    await agent.start()
    print(f"[{AGENT_NAME}] Running and listening for requests.")
    await asyncio.Future() # Keep alive

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s][%(name)s] %(message)s")
    asyncio.run(main())