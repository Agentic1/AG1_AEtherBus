+-----------------+           +--------------------+           +-------------------------------------+           +-------------------------+
| AetherDeck      |           | relay_server.py    |           |              Redis (AetherBus)      |           | muse_agent.py           |
| (Frontend UI)   |           | (AetherDeck Edge   |           |                                     |           | (MuseAgent Instance)    |
|                 | <---WS--->| Handler/Relay)     |           |                                     |           |                         |
+-----------------+           +--------------------+           +-------------------------------------+           +-------------------------+
       ^                            ^        ^                                  ^                                           ^
       | (UI Directives)            |        | (Publishes)                        |                                           |
       |                            |        |                                  |                                           |
       |                            |        | [AG1:agent:Muse1:inbox]          |                                           |
       |                            |        +----------------------------------+-------------------------------------------+ (Subscribes: BusAdapterV2)
       |                            |         (Agent's main inbox for ALL                                      |
       |                            |          incoming messages)                                               |
       |                            |                                                                          |
       |                            |                                                                          |
       |  (User Input/Events)       |                                                                          |
       |--------------------------->| [websocket_handler]                                                      |
                                    | (Parses AetherDeck JSON,                                                  |
                                    |  Normalizes to text+source,                                               |
                                    |  Creates Envelope with `reply_to` for UI Directives)                      |
                                    |                                                                          |
                                    |                                                                          |
                                    |                     [AG1:edge:aetherdeck:register] <-------------------+ (Publishes: `register_with_aetherdeck_handler`)
                                    |                       (Well-known stream for AetherDeck registrations) |
                                    |                                                                          |
                                    +------------------------------------------------------------------------+ (Subscribes: `handle_aetherdeck_registration_envelope`)
                                    (AetherDeck Edge Handler subscribes to this at startup to build its registry)
                                    |
                                    |
                                    |  (Receives Envelopes from MuseAgent for this specific UI)
[AG1:edge:aetherdeck:<user_id>:response] <-------------------------------------------------------------------+ (Publishes: final `_run_conversation` response using incoming `reply_to`)
(Unique stream for UI Directives to a specific AetherDeck WS client)                                     |
                                    |
                                    |
                                    | [listen_for_user_directives] subscribes to this per WS client
                                    | (Processes Envelope.content -> sends JSON UI Directive via WS)
                                    |
                                    |
                                    +---------------------------------------------------------------------------------------------------------------------------------------+
                                    (WebSocket Listener for AetherDeck clients: per client, subscribes to its unique response stream)


+-----------------+           +--------------------+           +-------------------------------------+
| Telegram        |           | tg_edge_handler.py |           |              Redis (AetherBus)      |
| (External App)  | <---TG--->| (Telegram Edge     |           |                                     |
|                 |           |   Handler)         |           |                                     |
+-----------------+           +--------------------+           +-------------------------------------+
       ^                            ^        ^                                  ^
       | (TG Msgs)                  |        | (Publishes)                        |
       |                            |        | [AG1:agent:Muse1:inbox]          |
       |                            |        +----------------------------------+-----------------------------
       |                            |         (Agent's main inbox for ALL                                      |
       |                            |          incoming messages)                                               |
       |                            |                                                                          |
       |                            |                     [AG1:edge:tg:register] <---------------------------+ (Publishes: `register_with_tg_handler`)
       |                            |                       (Well-known stream for Telegram registrations)     |
       |                            |                                                                          |
       |                            +------------------------------------------------------------------------+ (Subscribes: `handle_register_envelope`)
       |                            (TG Edge Handler subscribes to this at startup to launch bots)
       |                            |
       |                            | (Receives Envelopes from MuseAgent for this specific TG chat)
[AG1:edge:tg:<tg_handle>:response] <------------------------------------------------------------------------+ (Publishes: final `_run_conversation` response using incoming `reply_to`)
(Unique stream for TG replies to a specific bot handle/user)                                              |
                                    |
                                    | [handle_agent_reply] subscribes to this per TG bot (launched by registration)
                                    | (Processes Envelope.content -> sends text via TG API)
                                    |
                                    +---------------------------------------------------------------------------------------------------------------------------------------+
                                    (Telegram API Listener: per bot, subscribes to its unique response stream)


2. Step-by-Step Flow Explanation
Let's trace a user's message from AetherDeck, through MuseAgent, and back to the AetherDeck UI.
System Startup:
relay_server.py (AetherDeck Edge Handler) starts:
It initializes its aiohttp web server, starting a WebSocket endpoint (/).
It concurrently subscribes to the Redis stream AG1:edge:aetherdeck:register. This is where agents will announce their ability to handle AetherDeck traffic. It then waits for registrations.
muse_agent.py (MuseAgent) starts:
It initializes its BusAdapterV2 to listen on its primary inbox stream (AG1:agent:Muse1:inbox).
It registers with the Telegram handler by publishing an Envelope to AG1:edge:tg:register.
It registers with the AetherDeck handler by publishing an Envelope to AG1:edge:aetherdeck:register. This Envelope tells the AetherDeck handler: "I, Muse1, can handle AetherDeck traffic for the pattern 'all' (or a specific user_id), and you can send those events to my inbox AG1:agent:Muse1:inbox."
tg_edge_handler.py (Telegram Edge Handler) is running (usually separately):
It subscribes to AG1:edge:tg:register.
When it receives MuseAgent's registration, it launches a Telegram bot instance for @AG1_muse_bot and sets it up to send incoming Telegram messages to AG1:agent:Muse1:inbox and to listen for replies on AG1:edge:tg:@AG1_muse_bot:response.
AetherDeck User Initiates Conversation:
AetherDeck Client Connects to Relay:
An AetherDeck frontend client opens its page and establishes a WebSocket connection to ws://<relay_host>:<relay_port>/ (e.g., ws://localhost:8080/).
relay_server.py's websocket_handler is triggered. It generates a user_id (e.g., aetherdeck_guest_xyz) if not provided.
Initial UI Directive: The websocket_handler immediately sends a CREATE_WINDOW UI directive via the WebSocket back to the AetherDeck client, creating the "main_chat_window."
Reply Stream Subscription: It also creates a background task (listen_for_user_directives) that subscribes to a unique Redis stream for this specific AetherDeck client: AG1:edge:aetherdeck:aetherdeck_guest_xyz:response. This is where MuseAgent's UI directives for this client will arrive.
User Types a Message in AetherDeck:
The user types "Hello XYZ" in the AetherDeck command input and sends it.
AetherDeck sends a JSON event { "event_type": "user_chat_input", "text": "Hello XYZ" } via WebSocket to relay_server.py.
relay_server.py's handle_aetherdeck_event processes this:
User Echo: It immediately sends an APPEND_TO_TEXT_DISPLAY UI directive back to the AetherDeck client (e.g., User: Hello XYZ\n) to display the user's own message in the chat window.
Agent Routing: It looks up the registered agent for this user_id in its aetherdeck_registered_agents registry (finding Muse1's inbox: AG1:agent:Muse1:inbox).
Envelope Creation: It creates an Envelope:
role="user" (to signify user input)
user_id="aetherdeck_guest_xyz"
content={"text": "Hello XYZ", "source_channel": "aetherdeck"} (normalized text + source).
reply_to="AG1:edge:aetherdeck:aetherdeck_guest_xyz:response" (the unique stream the relay is listening on for directives back to this UI).
Publish to Agent: It publishes this Envelope to AG1:agent:Muse1:inbox.
MuseAgent Processes the Message:
muse_agent.py's BusAdapterV2 (listening on AG1:agent:Muse1:inbox) receives the Envelope.
muse_agent.handle_bus_envelope is invoked:
It sees env.role="user" and env.content.get("source_channel")="aetherdeck".
It extracts text_for_llm_input="Hello XYZ" and reply_to="AG1:edge:aetherdeck:aetherdeck_guest_xyz:response".
It queues the text_for_llm_input into a session-specific queue (self.input_queues[user_id]).
If it's a new session, it starts _run_conversation as a background task, passing the user_id and, crucially, the reply_to stream.
_run_conversation proceeds:
The User proxy agent pulls "Hello XYZ" from the queue.
Muse1 (the LLM agent) processes the input.
Coordinator generates the final natural language response (e.g., "We have a variety of tools...").
MuseAgent Sends Response (UI Directive):
When the Coordinator generates its final TextMessage, muse_agent.py in _run_conversation publishes an Envelope:
role="agent"
content={"text": "We have a variety of tools..."} (currently just text, but could be a full UI directive dict).
user_id="aetherdeck_guest_xyz"
reply_to="AG1:edge:aetherdeck:aetherdeck_guest_xyz:response" (using the reply_to it received from the initial message).
This Envelope is published to the reply_to stream: AG1:edge:aetherdeck:aetherdeck_guest_xyz:response.
Relay Receives UI Directive and Updates AetherDeck:
relay_server.py's listen_for_user_directives task (which is subscribed to AG1:edge:aetherdeck:aetherdeck_guest_xyz:response) receives the Envelope from MuseAgent.
Its directive_envelope_handler processes the envelope.content:
It sees content={"text": "We have a variety of tools..."}.
It detects it's a simple text response (not a full UI directive yet).
It wraps this text into an APPEND_TO_TEXT_DISPLAY UI directive for the main_chat_window's chat_log (e.g., Agent: We have a variety of tools...\n).
It sends this UI directive JSON via WebSocket to the AetherDeck client.
AetherDeck frontend receives the directive and updates the chat window, displaying the agent's response.