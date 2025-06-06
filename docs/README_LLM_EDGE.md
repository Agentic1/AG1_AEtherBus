# AG1 Core Bus – LLM Edge Handler (Azure)

This edge connector exposes Azure OpenAI models via the AG1 bus. It subscribes to
`AG1:edge:llm:requests`, forwards prompts to your Azure deployment, then publishes
responses back to the provided reply stream.

## Configuration
Credentials can be provided via environment variables or a config file.

### Environment Variables
- `AZURE_OPENAI_ENDPOINT` – Your Azure OpenAI endpoint URL
- `AZURE_OPENAI_API_KEY` – API key for the service
- `AZURE_OPENAI_DEPLOYMENT` – Chat completion deployment name
- `AZURE_OPENAI_API_VERSION` – API version (default `2024-02-15-preview`)

### Config File
You may supply a JSON or YAML file with the same keys using `--config`:

```yaml
endpoint: https://your-endpoint.openai.azure.com/
api_key: sk-...
deployment: gpt-35-turbo
api_version: 2024-02-15-preview
```

## Running
```
python -m AG1_AetherBus.handlers.llm_edge_handler --config /path/to/llm.yaml
```
If `--config` is omitted, the handler reads credentials from environment variables.
Ensure the Redis connection is configured via standard bus variables.

### Registration
Agents can announce their ability to use the LLM edge by publishing a
registration Envelope to `AG1:edge:llm:register`. The helper
`register_with_llm_handler` in `agent_bus_minimal.py` shows how to
construct this envelope.

## Message Flow
1. Client publishes a prompt to `AG1:edge:llm:requests` with a `reply_to` stream
2. The handler calls the Azure OpenAI Chat Completion API
3. The response is wrapped in an Envelope and published to the `reply_to` stream

---
Contributions are welcome! Please update documentation and add tests for any new
features.
