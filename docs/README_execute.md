# AEtherBus Execution Guide

This document explains how to run the core bus, register agents, and use the optional edge connectors and attestation layer.

## 1. Bus Overview

AEtherBus transports `Envelope` objects over Redis Streams. Producers publish to a stream and consumers subscribe using consumer groups. A typical flow:

```
[Producer] --publish--> AG1:<channel> --consume--> [Consumer]
```

Agents interact via their inboxes (e.g., `AG1:agent:muse:inbox`) and can reply to user-specific streams.

## 2. Agent Registry Handshake

`BusAdapterV2` automatically registers its `agent_id` when `start()` is called and removes it on `stop()`. Registration is stored in the Redis set `AG1:registry:agents`.
See `README_AGENT_REGISTRY.md` for more detail.

```
Agent start
   |
   | 1. register_agent()
   v
+----------------------+     +-----------------------+
| BusAdapterV2         | --> | AG1:registry:agents   |
+----------------------+     +-----------------------+
```

Call `is_registered(redis, agent_id)` to check if an ID is present.

## 3. Edge Connectors

### LLM Edge
- Provides access to Azure OpenAI models.
- Requests are read from `AG1:edge:llm:requests` and results published to the `reply_to` stream.
- Configure via `examples/llm_edge_config.yaml` or environment variables.

### Mail Edge
- Polls an IMAP inbox and sends agent replies via SMTP.
- Agents register on `AG1:edge:mail:register` with mailbox details. Example config: `examples/mail_edge_config.json`.

### Nostr Edge
- Connects to a Nostr relay and forwards notes mentioning a registered `pubkey`.
- Register by publishing to `AG1:edge:nostr:register`. Example config: `examples/nostr_edge_config.json`.

Edge handlers run as standalone processes:
```
python -m AG1_AetherBus.handlers.llm_edge_handler --config path/to/llm.yaml
python -m AG1_AetherBus.handlers.mail_edge.mail_edge_handler
python -m AG1_AetherBus.handlers.nostr_edge_handler
```

```
+---------+      +---------------------+      +------------+
| Client  | -->  | Edge Handler        | -->  | AG1 Bus    |
+---------+      +---------------------+      +------------+
```

## 4. Experimental Attestation

The folder `feature/experimental_attestation` contains optional helpers to sign envelopes with HMAC and record signatures in `attestations.db`.

Enable by importing `publish_with_attestation` and `subscribe_with_attestation` from `attestation_node.py`.

```
Producer --sign--> Bus --verify--> Consumer
             \                /
              \--ledger entry--/
```

## 5. Optional Encryption

You can combine attestation with encryption to keep message content private. The
recommended flow is documented in `README_encryption.md`:

1. **Sign then encrypt** – Sign the envelope first, store the ledger entry, then
   encrypt the serialized envelope with your chosen algorithm (e.g., AES-GCM).
2. **Decrypt then verify** – Receivers decrypt the payload and verify the
   signature against the ledger before processing.

```
Sender --sign & encrypt--> Bus --decrypt & verify--> Receiver
                \                            /
                 \------ ledger record ------/
```

This keeps the bus unchanged while adding confidentiality.

## 6. Running a Simple Agent

```
from AG1_AetherBus.bus_adapterV2 import BusAdapterV2
from AG1_AetherBus.envelope import Envelope
from redis.asyncio import Redis

async def handle(env, redis):
    print("got", env.content)

redis = Redis.from_url("redis://localhost:6379")
adapter = BusAdapterV2("muse1", handle, redis, patterns=["AG1:agent:muse1:inbox"])
await adapter.start()
```

Stop the adapter gracefully with `await adapter.stop()` which unregisters the ID.

---

For more details on each edge, see the dedicated README files in `docs/`.
