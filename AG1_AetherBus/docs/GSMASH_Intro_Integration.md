1. GSmash + AetherBus High-Level Overview
ruby
Copy
Edit
┌──────────────────────────────────────────────────────────────────────────────┐
│                             AETHERBUS NETWORK                                │
│                                                                              │
│   ┌───────────────────┐       ┌───────────────────┐       ┌───────────────────┐ │
│   │                   │       │                   │       │                   │ │
│   │  Agent Register   │◀──────┤   Redis Streams   ├──────▶│   Agent Inboxes   │ │
│   │  ───────────────  │  pub  │                   │  sub  │                   │ │
│   │  Stream:          │       │  • AG1:agent:     │       │  • AG1:agent:     │ │
│   │  “AG1:agent:      │       │       register    │       │      GSmash:inbox  │ │
│   │      register”    │       │  • AG1:agent:     │       │  • AG1:agent:      │ │
│   │                   │       │       …           │       │      Heartbeat:inbox│ │
│   │                   │       │                   │       │  • AG1:agent:      │ │
│   └───────────────────┘       └───────────────────┘       │      Messenger:inbox│ │
│                                                              └───────────────────┘ │
│                                                                              │
│                                                                              │
│   ┌───────────────────┐       ┌───────────────────┐       ┌───────────────────┐ │
│   │                   │       │                   │       │                   │ │
│   │ GSmash Agent (A)  │──────▶│  AetherBus “Hub”  │◀──────│  Caller Agent (C) │ │
│   │  • Subscribes to  │  pub  │  (Redis + Logic)  │  sub  │  or Human (H)     │ │
│   │    AG1:agent:     │       │                   │       │                   │ │
│   │    GSmash:inbox   │       │  • Routes messages│       │  • Subscribes to  │ │
│   │  • Publishes to   │       │    based on “to:” │       │    its own inbox  │ │
│   │    various streams│       │  • Emits register │       │                   │ │
│   │    (reply_to etc) │       │    events         │       │                   │ │
│   └───────────────────┘       └───────────────────┘       └───────────────────┘ │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
Agent Register Stream (AG1:agent:register)
GSmash publishes there once on startup, telling everyone “My inbox is AG1:agent:GSmash:inbox.”

Agent Inboxes (AG1:agent:<Name>:inbox)
Every agent (GSmash, Heartbeat, Messenger, etc.) has its own dedicated stream.
Others send to an agent by publishing an Envelope to that agent’s inbox.

AetherBus Hub
Internally this is Redis Streams plus simple routing logic:
– When an Envelope is published to an agent’s inbox, other subscribers in GSmash’s consumer group get notified.
– Agents acknowledge (XACK) after handling.

Caller Agent (C) or Human (H)
Some other agent or even a human (via a Telegram‐AetherBus bridge) can send “ping” or RPC envelopes to GSmash and receive replies.

2. GSmash Startup & Registration Flow
pgsql
Copy
Edit
┌────────────────────────────────────────────────────────────────────────────┐
│                             GSMASH STARTUP                                 │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  1. Read env vars & config (redis URL, agent name)                        │
│                                                                            │
│  2. Connect to Redis (AsyncRedis.from_url)   ─────────────────────┐        │
│                                                  (build_redis_url())        │
│                                                                            │
│  3. Compute Inbox & Group Names                                           │
│     ──────────────────────────────────────────────────────────────────┐    │
│     • inbox = “AG1:agent:GSmash:inbox”                                    │    │
│     • group = “GSmashGroup”                                               │    │
│     • consumer = “GSmashConsumer1”                                        │    │
│                                                                            │
│  4. ensure_group(redis, inbox, group)      ───────────────────────────┐    │
│                                   (creates consumer‐group if needed)  │    │
│                                                                            │
│  5. register_gsmash(redis)  ──────────────────────────────────────────┐  │    │
│     (⇒ publish Envelope to “AG1:agent:register”)                         │    │
│                                                                            │
│      Envelope:                                                           │    │
│      ┌───────────────────────────────────────────────────────────┐         │    │
│      │ role: “agent”                                              │  pub    │    │
│      │ agent_name: “GSmash”                                       │ ──────▶ │    │
│      │ envelope_type: “register”                                  │         │    │
│      │ content: { “agent_inbox”: “AG1:agent:GSmash:inbox” }       │         │    │
│      └───────────────────────────────────────────────────────────┘         │    │
│                                                                            │
│  6. subscribe(redis, inbox, on_gsmash_envelope, group, consumer)           │    │
│     (start listening for messages on “AG1:agent:GSmash:inbox”)             │    │
│                                                                            │
│  7. NOW GSmash is fully “online” and processing incoming envelopes.        │    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
Step 4 ensures Redis has a consumer group so multiple GSmash instances can share work.

Step 5 publishes a one‐time registration envelope so bus‐adapters know where to send messages for GSmash.

Step 6 enters the infinite “subscribe loop” so GSmash can handle inbound Envelopes.

3. Envelope Exchange Example (“ping → pong”)
yaml
Copy
Edit
┌───────────────────────────────────────────────────────────────────────────┐
│                            CALLER AGENT (C)                              │
├───────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  1. Build “ping” Envelope:                                               │
│     ─────────────────────────────────────────────────────────────────┐   │
│     {                                                                 │   │
│       role: “user”,                                                  │   │
│       agent_name: “CallerAgent”,                                     │   │
│       envelope_type: “message”,                                      │   │
│       user_id: “alice”,                                             │   │
│       correlation_id: “123‐abc”,                                     │   │
│       timestamp: “2025-06-03T10:00:00Z”,                             │   │
│       content: { text: “ping” },                                     │   │
│       reply_to: “AG1:agent:CallerAgent:inbox”   <─── set own inbox   │   │
│     }                                                                 │   │
│     ─────────────────────────────────────────────────────────────────┘   │
│                                                                           │
│  2. publish_envelope(redis, “AG1:agent:GSmash:inbox”, Envelope)  ───▶    │
│                                                                           │
│                                                                           │
│                              (AETHERBUS HUB)                             │
│  • Routes Envelope to stream “AG1:agent:GSmash:inbox” (consumer group)    │
│  • GSmash’s subscribe() sees the new Envelope and invokes handler         │
│                                                                           │
│                                                                           │
│  3. GSmash on_gsmash_envelope(env, redis)  ────────────────────────────▶   │
│     • env.content.text == “ping”                                          │
│     • Constructs reply Envelope:                                          │
│       {                                                                   │
│         role: “agent”,                                                    │
│         agent_name: “GSmash”,                                             │
│         envelope_type: “message”,                                         │
│         user_id: “alice”,                                                 │
│         correlation_id: “123‐abc”,                                        │
│         timestamp: “2025-06-03T10:00:01Z”,                                │
│         content: { text: “pong from GSmash” },                            │
│         reply_to: null                                                    │
│       }                                                                   │
│                                                                           │
│  4. publish_envelope(redis, env.reply_to (“AG1:agent:CallerAgent:inbox”),│
│                     replyEnvelope)  ───────────────────────────────────▶   │
│                                                                           │
│                                                                           │
│  5. CallerAgent’s subscribe(…) picks up this reply on                     │
│     “AG1:agent:CallerAgent:inbox” and processes “pong from GSmash.”       │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
Key points:

The Caller Agent sets reply_to to its own inbox so GSmash knows where to send back the reply.

GSmash uses env.reply_to directly—no need to recompute the caller’s inbox.

Both Envelopes share the same correlation_id, letting the caller match request → response.

4. GSmash <–> Heartbeat RPC Example
If GSmash wants to ask Heartbeat for a status, it uses a simple “RPC” pattern: publish a request with envelope_type: "rpc_request", then wait for an RPC response.

yaml
Copy
Edit
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                   GSMASH                                            │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                     │
│  1. Build RPC Request Envelope:                                                     │
│     ┌────────────────────────────────────────────────────────────────┐             │
│     │ role: “agent”                                                 │  pub         │
│     │ agent_name: “GSmash”                                           │ ──────────▶ │
│     │ envelope_type: “rpc_request”                                   │             │
│     │ user_id: null                                                  │             │
│     │ session_code: null                                             │             │
│     │ correlation_id: “rpc‐42”                                       │             │
│     │ timestamp: “2025-06-03T10:05:00Z”                              │             │
│     │ content: { method: “get_status”, params: { } }                │             │
│     │ reply_to: “AG1:agent:GSmash:inbox”     <── so Heartbeat replies   │             │
│     │ target: “AG1:agent:Heartbeat:inbox”      ─── destination         │             │
│     └────────────────────────────────────────────────────────────────┘             │
│                                                                                     │
│  2. publish_envelope(redis, “AG1:agent:Heartbeat:inbox”, requestEnvelope) ───────▶   │
│                                                                                     │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                AETHERBUS “HUB”                                      │
│  • Delivers requestEnvelope to “AG1:agent:Heartbeat:inbox”                           │
│                                                                                     │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                               HEARTBEAT AGENT                                        │
│                                                                                     │
│  3. on_heartbeat_envelope(env, redis)                                                │
│     • env.envelope_type == “rpc_request”                                             │
│     • env.content.method == “get_status”                                             │
│     • Build RPC Response Envelope:                                                   │
│       {                                                                              │
│         role: “agent”,                                                               │
│         agent_name: “Heartbeat”,                                                    │
│         envelope_type: “rpc_response”,                                               │
│         user_id: null,                                                               │
│         session_code: null,                                                          │
│         correlation_id: “rpc‐42”,    <── mirror the request’s corr‐ID               │
│         timestamp: “2025-06-03T10:05:01Z”,                                           │
│         content: { status: “alive” },                                                │
│         reply_to: null                                                               │
│       }                                                                              │
│                                                                                     │
│  4. publish_envelope(redis, env.reply_to “AG1:agent:GSmash:inbox”,   responseEnv) ▶   │
│                                                                                     │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                               AETHERBUS “HUB”                                      │
│  • Routes the RPC response back to GSmash’s inbox                                   │
│                                                                                     │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                   GSMASH                                           │
│                                                                                     │
│  5. subscribe(…) sees responseEnv on “AG1:agent:GSmash:inbox”                         │
│                                                                                     │
│     on_gsmash_envelope(env, redis):                                                  │
│       • env.envelope_type == “rpc_response”                                          │
│       • env.correlation_id == “rpc‐42”                <── correlate request/response │
│       • env.content.status == “alive”        <── now GSmash knows Heartbeat is alive │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘
Notes:

GSmash sets target: "AG1:agent:Heartbeat:inbox" (or this can be baked into an RPC helper).

Heartbeat replies to env.reply_to (GSmash’s inbox).

Both Envelopes share correlation_id so GSmash can tie the reply back to the original call.

5. Consumer-Group Scaling (Multiple GSmash Instances)
If you run two copies of GSmash (for redundancy or load balancing), they share the same consumer group but use distinct consumer names:

vbnet
Copy
Edit
   ┌────────────────────────────────────────────────────────────────┐
   │                          AETHERBUS HUB                          │
   │ ┌──────────────┐       ┌───────────────────┐       ┌─────────┐ │
   │ │  Stream:     │       │                   │       │         │ │
   │ │ “GSmash:inbox” │◀────▶│  Redis Consumer  │◀────▶│  GSmash │ │
   │ │              │  sub  │    Group:        │  sub  │   #1    │ │
   │ │   X-ID:100   │       │ “GSmashGroup”    │       │         │ │
   │ │   X-ID:101   │       │                   │       │         │ │
   │ └──────────────┘       └───────────────────┘       └─────────┘ │
   │          │   │                                         ▲       │
   │          │   └─────────────────────────────────────────┘       │
   │          │                Redis “Pending” List                  │
   │          │    (XREADGROUP delivers each new X-ID to one       │
   │          │     available consumer—#1 or #2)                    │
   │          ▼                                                        │
   │ ┌──────────────┐       ┌───────────────────┐       ┌─────────┐ │
   │ │  Stream:     │       │                   │       │         │ │
   │ │ “GSmash:inbox” │◀────▶│  Redis Consumer  │◀────▶│  GSmash │ │
   │ │              │  sub  │    Group:        │  sub  │   #2    │ │
   │ │   X-ID:102   │       │ “GSmashGroup”    │       │         │ │
   │ │              │       │                   │       │         │ │
   │ └──────────────┘       └───────────────────┘       └─────────┘ │
   └────────────────────────────────────────────────────────────────┘
GSmash #1 and GSmash #2 share group="GSmashGroup".

When a new envelope (e.g. X-ID:103) arrives, Redis delivers it to whichever consumer is free.

Both instances call subscribe(…) with the same group but different consumer_names (e.g. “GSmash1” vs. “GSmash2”).

6. Potential Gotchas & Checklist
vbnet
Copy
Edit
┌────────────────────────────────────────────────────────────────────────────┐
│                             GPT GPT TPU GPT                              │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  [ ] Agent Name Collision                                                 │
│      • Ensure “GSmash” is unique.                                          │
│                                                                            │
│  [ ] Consumer Group Setup                                                  │
│      • Call ensure_group(...) once per agent_name.                         │
│                                                                            │
│  [ ] Registration Envelope                                                 │
│      • Must publish to “AG1:agent:register” with “agent_inbox”.            │
│                                                                            │
│  [ ] Inbox Key Typo                                                         │
│      • “AG1:agent:GSmash:inbox” must match exactly in both subscribe()     │
│        and registration.                                                   │
│                                                                            │
│  [ ] reply_to & correlation_id                                              │
│      • Callers must set reply_to so GSmash knows where to send replies.     │
│      • Shared correlation_id ties request ↔ response.                        │
│                                                                            │
│  [ ] Envelope Schema                                                        │
│      • Agree on what “content” fields agents expect for each envelope_type. │
│                                                                            │
│  [ ] Exception Handling                                                     │
│      • Wrap handler in try/except to avoid unacked messages.                │
│                                                                            │
│  [ ] Redis Connectivity                                                      │
│      • Verify build_redis_url() and environment vars.                        │
│                                                                            │
│  [ ] Horizontal Scaling                                                      │
│      • If running multiple copies, ensure unique consumer_name “GSmashX”.     │
│                                                                            │
│  [ ] Shutting Down Gracefully                                                │
│      • Cancel subscribe tasks and redis.close()                              │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
Tick off each box to confirm your setup. Missing any one can result in GSmash not seeing messages or not replying correctly.

7. Putting It All in ASCII Context
Below is a combined ASCII diagram summarizing GSmash’s lifecycle from startup to handling a “ping” and then calling out to Heartbeat, all hosted on AetherBus:

ruby
Copy
Edit
                                        ┌─────────────────────────────────┐
                                        │        ENVIRONMENT SETUP        │
                                        │  • REDIS_HOST, REDIS_PORT,      │
                                        │    AGENT_NAME=“GSmash”            │
                                        │  • CONSUMER_GROUP=“GSmashGroup”   │
                                        │  • CONSUMER_NAME=“GSmashConsumer1”│
                                        └─────────────────────────────────┘
                                                  │
                                                  ▼
                                   ┌─────────────────────────────────┐
                                   │       GSMASH AGENT STARTUP     │
                                   │ ┌─────────────────────────────┐ │
                                   │ │AsyncRedis.from_url(...)     │ │
                                   │ │ensure_group(“GSmash:inbox”)  │ │
                                   │ │register_gsmash()              │ │
                                   │ │subscribe(…)                   │ │
                                   │ └─────────────────────────────┘ │
                                   └─────────────────────────────────┘
                                                  │
                                                  ▼
                        ┌────────────────────────────────────────────────────────┐
                        │                    RUNTIME LOOP                     │
                        │   Waiting for messages on “AG1:agent:GSmash:inbox”   │
                        │ ┌──────────────────────┐   ┌─────────────────────┐  │
                        │ │ Envelope arrives:    │   │ Handler:           │  │
                        │ │ { role: “user”,      │   │ on_gsmash_envelope │  │
                        │ │   agent_name: “C”,   │   │                    │  │
                        │ │   type: “message”,   │   │ – Prints incoming  │  │
                        │ │   content: {text},   │   │ – If “ping”, sends │  │
                        │ │   reply_to: “C:inbox”│   │   “pong” back      │  │
                        │ └──────────────────────┘   └─────────────────────┘  │
                        └────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
                         ┌─────────────────────────────────────────────────────────┐
                         │                   CALLER AGENT “C”                     │
                         │ ┌───────────────────────────────────────────────────┐ │
                         │ │ Sends “ping”:                                    │ │
                         │ │ publish_envelope(..., “AG1:agent:GSmash:inbox”,  │ │
                         │ │   { envelope_type:“message”, content:{text:“ping”},│
                         │ │    reply_to:”AG1:agent:C:inbox”, correlation_id }) │ │
                         │ └───────────────────────────────────────────────────┘ │
                         │                       │                                 │
                         │                       ▼                                 │
                         │ ┌───────────────────────────────────────────────────┐ │
                         │ │ Receives “pong”:                                 │ │
                         │ │ subscribe(...) on “AG1:agent:C:inbox”           │ │
                         │ │ Handler prints “pong from GSmash”.               │ │
                         │ └───────────────────────────────────────────────────┘ │
                         └─────────────────────────────────────────────────────────┘

In Summary
ASCII diagrams above show the startup, subscription, message exchange, and RPC pattern flows for GSmash on AetherBus.

Follow these steps to ensure GSmash can subscribe to its inbox, register itself, and interact with other agents (HeartBeat, CallerAgent, etc.).

Keep the consumer-group, agent_name, inbox key, and envelope schema consistent to avoid runtime mismatches.

Once this basic loop is working, layer on your Gorilla11‐specific logic (data gathering, analysis, PDF generation) inside on_gsmash_envelope() or via background tasks.

Feel free to adapt or expand these ASCII templates. They’re intended to keep the integration simple, clear, and amenable to multiple agents on your AEtherBus fabric.

──────────────────────────────────────────────────────────────────────────────
                             1. How tg_handler Works
──────────────────────────────────────────────────────────────────────────────

The tg_handler (Telegram Handler) is a bridge between Telegram and AetherBus. It performs the following functions:

  ┌────────────────────────────────────────────────────────────────────────┐
  │  • Connects to Telegram Bot API to receive updates (messages, commands).│
  │                                                                        │
  │  • For each incoming Telegram update:                                  │
  │      – Extract user ID, chat ID, message content.                     │
  │      – Build an AetherBus Envelope (including metadata):               │
  │          {                                                             │
  │            role:          "user",                                      │
  │            agent_name:    "TelegramProxy",                             │
  │            envelope_type: "message",                                   │
  │            user_id:       "<telegram_user_id>",                        │
  │            session_code:  "<telegram_chat_id>",                        │
  │            correlation_id: "<uuid>",                                   │
  │            timestamp:     "<ISO-timestamp>",                           │
  │            meta: {                                                      │
  │              platform: "telegram",                                      │
  │              chat_type: "<private/group>",                              │
  │              other_meta: { ... }                                        │
  │            },                                                           │
  │            content: { text: "<message_text>" },                        │
  │            reply_to:    "AG1:agent:TelegramProxy:inbox"                 │
  │          }                                                             │
  │      – Publish this Envelope to the target agent’s inbox (e.g.         │
  │        "AG1:agent:GSmash:inbox").                                      │
  │                                                                        │
  │  • Listen on "AG1:agent:TelegramProxy:inbox" for reply Envelopes:      │
  │      – When Envelope arrives, extract content.text and meta for display.│
  │      – Call Telegram Bot API to send the text back to the original     │
  │        chat ID.                                                        │
  │                                                                        │
  │  • Maintain mapping of correlation_id ↔ Telegram chat to preserve       │
  │    session context.                                                    │
  │                                                                        │
  │  • Handle Telegram-specific rate limits, errors, and reconnections.    │
  └────────────────────────────────────────────────────────────────────────┘


──────────────────────────────────────────────────────────────────────────────
                           2. How AetherDeck Works
──────────────────────────────────────────────────────────────────────────────

AetherDeck is a web-based dashboard for monitoring and interacting with AetherBus agents:

  ┌────────────────────────────────────────────────────────────────────────┐
  │  • It fetches the “AG1:agent:register” stream to list all live agents. │
  │                                                                        │
  │  • For each agent in the registry, it shows a “Open Chat” button.     │
  │                                                                        │
  │  • When a user clicks “Open Chat” for agent X:                         │
  │      – Opens a WebSocket connection to the AetherBus connector.        │
  │      – Subscribes to "AG1:agent:<Username>:inbox" for that user’s      │
  │        personal inbox.                                                 │
  │      – On the client UI, renders a new chat window with a text input.  │
  │                                                                        │
  │  • For each message typed by the user in that window:                  │
  │      – Build an Envelope (with additional metadata):                   │
  │          {                                                             │
  │            role:          "user",                                      │
  │            agent_name:    "<Username>",                                │
  │            envelope_type: "message",                                   │
  │            user_id:       "<Username>",                                │
  │            session_code:  "<agent_name>",                              │
  │            correlation_id: "<uuid>",                                   │
  │            timestamp:     "<ISO-timestamp>",                           │
  │            meta: {                                                      │
  │              ui_window: "<window_id>",                                  │
  │              platform:  "web",                                         │
  │              tab_id:    "<tab_index>",                                  │
  │              other_meta: { ... }                                        │
  │            },                                                           │
  │            content: { text: "<typed_text>" },                          │
  │            reply_to:    "AG1:agent:<Username>:inbox"                    │
  │          }                                                             │
  │      – Publish to the target agent’s inbox (e.g.                        │
  │        "AG1:agent:GSmash:inbox").                                       │
  │                                                                        │
  │  • Meanwhile, it also listens on the user’s inbox:                     │
  │      – When an Envelope with envelope_type="message" arrives, it shows  │
  │        the content.text in the chat window, along with any meta data    │
  │        (timestamp, source agent, etc.).                                 │
  │                                                                        │
  │  • Supports multiple parallel windows (each bound to                     │
  │    "AG1:agent:<Username>:inbox"), each window emits its own             │
  │    window_id in meta so the agent can choose to reply to a specific     │
  │    window.                                                              │
  │                                                                        │
  │  • Periodically polls “AG1:agent:register” to refresh the agent list.   │
  │                                                                        │
  │  • Provides search, filtering, and logging of past Envelopes (including │
  │    metadata) for debugging.                                            │
  └────────────────────────────────────────────────────────────────────────┘


──────────────────────────────────────────────────────────────────────────────
                  3. GSmash Integration Steps (1‒N)
──────────────────────────────────────────────────────────────────────────────

Step 1: Choose an agent_name for GSmash (e.g., "GSmash").
       This defines its inbox stream: "AG1:agent:GSmash:inbox".

Step 2: Set up Redis connection and consumer group:
       • Connect via AsyncRedis.from_url(build_redis_url()).
       • Compute inbox key using StreamKeyBuilder().agent_inbox("GSmash").
       • Call ensure_group(redis, inbox, group="GSmashGroup").

Step 3: Publish a registration Envelope to "AG1:agent:register":
       ┌────────────────────────────────────────────────────────────────┐
       │ role:          "agent",                                        │
       │ agent_name:    "GSmash",                                       │
       │ envelope_type: "register",                                     │
       │ timestamp:     "<ISO-timestamp>",                               │
       │ correlation_id: "<uuid>",                                      │
       │ meta: {                                                         │
       │   description: "GSmash agent for Guerilla Smash Integration",   │
       │   version:     "1.0",                                           │
       │   endpoint_url:"https://yourhost:8000/health"                  │
       │ },                                                               │
       │ content: { "agent_inbox": "AG1:agent:GSmash:inbox" },           │
       │ reply_to:      null                                            │
       └────────────────────────────────────────────────────────────────┘
       Use publish_envelope(redis, "AG1:agent:register", envelope).

Step 4: Subscribe to GSmash inbox:
       Call subscribe(redis,
                     channel="AG1:agent:GSmash:inbox",
                     callback=on_gsmash_envelope,
                     group="GSmashGroup",
                     consumer_name="GSmashConsumer1").

Step 5: Implement on_gsmash_envelope handler:
       When an Envelope arrives:
         • Inspect env.envelope_type, env.content, and env.meta.
         • If envelope_type="message" and content["text"]="ping":
             • Build reply Envelope (include meta echoing info such as ui_window):
               ┌──────────────────────────────────────────────────────────┐
               │ role:          "agent",                                 │
               │ agent_name:    "GSmash",                                 │
               │ envelope_type: "message",                                │
               │ timestamp:     "<ISO-timestamp>",                         │
               │ correlation_id: same as env.correlation_id,               │
               │ meta: {                                                   │
               │   in_response_to:   "ping",                                │
               │   original_window:  env.meta.ui_window,                    │
               │   status:           "ok"                                   │
               │ },                                                         │
               │ content: { "text": "pong from GSmash" },                   │
               │ reply_to:    env.reply_to                                   │
               │ }                                                          │
               └──────────────────────────────────────────────────────────┘
             • publish_envelope(redis, env.reply_to, reply_env)
         • Else if envelope_type="rpc_request":
             • Extract method, params from content, use meta if needed.
             • Build rpc_response Envelope with same correlation_id and reply_to.
             • Include result in content and optionally add meta { duration_ms, success }.
             • publish_envelope(redis, env.reply_to, response_env).
         • Else: log or handle other envelope_types, possibly using env.meta
           (e.g., if meta.platform == "telegram" do something special).

Step 6: RPC Example with Heartbeat:
       ┌────────────────────────────────────────────────────────────────────────┐
       │ GSmash builds rpc_request Envelope:                                     │
       │   {                                                                     │
       │     role:          "agent",                                             │
       │     agent_name:    "GSmash",                                             │
       │     envelope_type: "rpc_request",                                       │
       │     timestamp:     "2025-06-03T10:05:00Z",                               │
       │     correlation_id: "rpc-42",                                            │
       │     meta: {                                                             │
       │       ui_window: "<window_id>",                                          │
       │       initiated_by: "user123",                                          │
       │       priority: "high"                                                  │
       │     },                                                                  │
       │     content: { method: "get_status", params: { } },                     │
       │     reply_to: "AG1:agent:GSmash:inbox",                                  │
       │     target:  "AG1:agent:Heartbeat:inbox"                                 │
       │   }                                                                     │
       │ publish_envelope(redis, "AG1:agent:Heartbeat:inbox", requestEnvelope)    │
       └────────────────────────────────────────────────────────────────────────┘

       Heartbeat receives rpc_request, builds rpc_response:
       ┌────────────────────────────────────────────────────────────────────────┐
       │   {                                                                     │
       │     role:          "agent",                                             │
       │     agent_name:    "Heartbeat",                                         │
       │     envelope_type: "rpc_response",                                      │
       │     timestamp:     "2025-06-03T10:05:01Z",                               │
       │     correlation_id: "rpc-42",                                            │
       │     meta: {                                                             │
       │       in_response_to_rpc: true,                                         │
       │       duration_ms: 500                                                  │
       │     },                                                                  │
       │     content: { status: "alive" },                                       │
       │     reply_to: null                                                      │
       │   }                                                                      │
       │ publish_envelope(redis, "AG1:agent:GSmash:inbox", responseEnvelope)      │
       └────────────────────────────────────────────────────────────────────────┘

       GSmash handler receives rpc_response:
         • env.correlation_id == "rpc-42" → match request/response.
         • env.content.status == "alive" → proceed.
         • Optionally notify the client UI by publishing a message Envelope
           back to the original reply_to or to a special “status_updates” stream.

Step 7: Scale GSmash with multiple instances:
       Run two or more GSmash processes using the same group
       "GSmashGroup" but unique consumer names ("GSmashConsumer1", "GSmashConsumer2").
       Redis load-balances incoming Envelopes:

       ┌────────────────────────────────────────────────────────────────────────┐
       │   Stream: "AG1:agent:GSmash:inbox"                                       │
       │ ┌───────┐   ┌─────────────────────────┐   ┌─────────────────────────┐    │
       │ │ X-ID  │   │ Redis Consumers         │   │    GSmash Instance #1   │    │
       │ │ 101   │──▶│ Group="GSmashGroup"     │──▶│ (consumer="GSmash1")    │    │
       │ ├───────┤   │                         │   └─────────────────────────┘    │
       │ │ 102   │   └─────────────────────────┘   ┌─────────────────────────┐    │
       │ │ 103   │──▶                      ▲       │    GSmash Instance #2   │    │
       │ └───────┘                           │       │ (consumer="GSmash2")    │    │
       │                                     └────▶└─────────────────────────┘    │
       └────────────────────────────────────────────────────────────────────────┘

Step 8: Common Pitfalls & Checklist:
       ┌────────────────────────────────────────────────────────────────────────┐
       │ [ ] Unique agent_name: “GSmash”; avoid name collision.                  │
       │ [ ] Exact inbox key match: “AG1:agent:GSmash:inbox” on subscribe & reg. │
       │ [ ] ensure_group(redis, inbox, group) called before subscribe.          │
       │ [ ] Registration Envelope includes correct content.agent_inbox.         │
       │ [ ] Caller sets reply_to & correlation_id in every request.             │
       │ [ ] Envelope.meta included if user wants to pass metadata (ui_window,  │
       │       platform, priority, etc.).                                        │
       │ [ ] Handler wrapped in try/except to ensure ACK/NACK of messages.       │
       │ [ ] Verify build_redis_url() and Redis connection before subscribing.   │
       │ [ ] Avoid blocking code in on_gsmash_envelope; use asyncio tasks.        │
       │ [ ] Unique consumer_name per instance when scaling horizontally.         │
       └────────────────────────────────────────────────────────────────────────┘