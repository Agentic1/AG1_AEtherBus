# AG1 Core Bus – Nostr Edge Handler

This edge connector links the Nostr network to the AG1 bus. It listens for notes
mentioning a registered public key and forwards them to the agent's inbox. Agent
replies published to the corresponding response stream are sent back to the
relay.

## Registration
Agents register by publishing an Envelope to `AG1:edge:nostr:register` with the
Nostr details:

```json
{
  "envelope_type": "register",
  "agent_name": "MuseNostr",
  "content": {
    "pubkey": "npub1...",
    "relay": "wss://relay.damus.io"
  }
}
```

An example config file is available at `examples/nostr_edge_config.json`.

On registration the handler starts a WebSocket listener on the relay and
subscribes to the agent reply channel `AG1:edge:nostr:<pubkey>:response`.

## Message Flow
1. Notes on the relay tagged with the agent's `pubkey` are received.
2. Each note is published to `AG1:agent:<agent_name>:inbox` with the reply stream
   set to `AG1:edge:nostr:<sender_pubkey>:response`.
3. Agent replies on that stream are sent as simple notes back to the relay.

## Running
```
python -m AG1_AetherBus.handlers.nostr_edge_handler
```
Ensure the Redis and relay credentials are configured. The handler requires the
`websockets` package for network communication.
