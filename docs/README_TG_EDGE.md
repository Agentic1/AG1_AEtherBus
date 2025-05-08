# AG1 Core Bus – Telegram Edge Handler

(Moved to docs/ from project root.)

## Overview
The AG1 Muse Telegram Edge Handler (`tg_edge_handler.py`) is a dynamic, production-ready bridge between Telegram and the AG1 multi-agent system. It enables seamless, scalable, and modular integration of multiple Telegram bots (agents) with the AG1 core bus, supporting both private and group chat scenarios.

---

## Key Features

- **Dynamic Multi-Bot Support:**
  - Register and launch multiple Telegram bots at runtime, each with its own token and agent identity.
  - No restart required—bots can be added or removed on the fly.

- **Envelope-Based Messaging:**
  - All communication uses a strict Envelope schema for reliability, extensibility, and modularity.
  - Supports both user-to-agent and agent-to-user flows, including group and private chats.

- **No Message Loops:**
  - Agent-to-agent rebroadcasting is disabled by default, preventing infinite loops and message storms.

- **Robust Agent Registry:**
  - Maintains live status of all registered agents and running bot instances, with periodic status reporting.

- **Clean Separation of Concerns:**
  - Handler logic, registration, and bus communication are modular and maintainable.

---

## Architecture

```
┌──────────────┐      ┌─────────────┐      ┌──────────────┐
│ Telegram App │<--->│ Telegram Bot│<--->│ AG1 Edge Bus │<---> AG1 Agents
└──────────────┘      └─────────────┘      └──────────────┘
```
- Each agent gets its own Telegram bot, managed by the edge handler.
- All message routing is performed via the AG1 core bus using Envelopes and Redis Streams.

---

## Usage

### 1. **Start the Edge Handler**
```bash
python core_bus/tg_edge_handler.py
```

### 2. **Register Agents**
- Each agent (e.g., Muse1, Muse2) should send a registration envelope with its `tg_handle` and unique bot token (`key`).
- Registration can be triggered by running the agent process or via a registration script.

### 3. **Interact via Telegram**
- Users can message any registered bot in private or group chats.
- Agent replies are routed back to the correct chat via the edge handler.

---

## Configuration
- **Bot Tokens:**
  - Store tokens securely in agent config files (e.g., `configs/muse1.json`) or environment variables.
- **Redis Connection:**
  - Configurable via environment variables or `.env` file.
- **Broadcasting:**
  - `ENABLE_AGENT_BROADCAST = False` by default for safety.

---

## Advanced Features & Recommendations
- **Graceful Shutdown:**
  - Implement logic to remove bots from the registry and gracefully close sessions on `/stop` or unregister.
- **Logging & Monitoring:**
  - Integrate with Python `logging` and external monitoring for production.
- **Security:**
  - Add rate limiting and input validation for abuse prevention.
- **Scaling:**
  - For large deployments, consider running each bot in its own process or container.

---

## Troubleshooting
- **Bot Conflict Error:** Ensure each bot token is unique and only one polling instance per token is running.
- **No Replies:** Check that `reply_chat_id` is set and agent replies are correctly routed.
- **Redis Issues:** Verify Redis connection and stream configuration.

---

## TODO / Enhancement Roadmap

- **Resource Management & Cleanup:**
  - Implement graceful shutdown for bots (remove from registry, close sessions, cancel polling).
- **Error Handling & Resilience:**
  - Replace print statements with Python logging. Add retries and alerting for failures.
- **Configuration & Secrets:**
  - Support environment variables and/or secrets manager for tokens/configs.
- **Concurrency & Scaling:**
  - Consider process/container-per-bot for large deployments.
- **Observability & Monitoring:**
  - Add HTTP healthcheck endpoint and integrate with external monitoring (Prometheus, Grafana, etc).
- **Testing & CI/CD:**
  - Add unit/integration tests and set up CI/CD pipeline.
- **Security:**
  - Add rate limiting and input validation.
- **Documentation:**
  - Add high-level docstrings and architecture diagrams.

### Heartbeat & Health Check (Planned)
- Implement a heartbeat/health check system:
  - Each agent should periodically send a heartbeat message to the edge handler (or core bus).
  - The edge handler should monitor for missed heartbeats and mark agents as down if unresponsive.
  - Consider global health checks for all bots/agents, with status reporting and optional auto-restart.
- **Design question:** Should health management be handled by a dedicated "autoagent" (smart supervisor agent), or should the edge handler remain simple/dumb and only report status? Both approaches have merits:
  - *Autoagent*: More flexible, can handle restarts, escalation, and richer policy.
  - *Dumb handler*: Simpler, less risk of bugs/loops, but less autonomous recovery.

## Contributing
- PRs and suggestions are welcome! Please add tests and update documentation for any new features.

---

## License
This project is part of the AG1 Muse ecosystem. See main project license for details.
