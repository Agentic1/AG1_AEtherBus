# AEtherBus V1.1

[![Python Version](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A high-performance, Redis-based message bus for agent communication, featuring envelope-based routing, MCP integration, and real-time message handling.

## 🚀 Features

- **Envelope-based Messaging**: Structured message format with headers and payload
- **MCP Integration**: Seamless tool integration via MCP bridge
- **WebSocket Support**: Real-time communication for web clients
- **Modular Handlers**: Extensible architecture for different edge cases
- **Asynchronous**: Built with `asyncio` for high concurrency

## 📦 Installation

```bash
# Using Poetry (recommended)
poetry add aetherbus

# Or with pip
pip install aetherbus
🏁 Quick Start
python
CopyInsert
from aetherbus import AgentBus
import asyncio

async def main():
    bus = AgentBus()
    await bus.connect()
    
    # Subscribe to messages
    async for envelope in bus.subscribe("user.123.inbox"):
        print(f"Received: {envelope}")

asyncio.run(main())
🏗 Architecture
CopyInsert
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Edge Handlers ├───►│   Core Bus     │◄────┤   MCP Bridge   │
│  (Telegram, WS) │    │  (Redis-based)  │     │  (Tool Manager) │
└─────────────────┘    └─────────────────┘     └─────────────────┘
📚 Documentation
Getting Started
API Reference
MCP Integration
WebSocket API
🤝 Contributing
Fork the repository
Create a feature branch (git checkout -b feature/amazing-feature)
Commit your changes (git commit -m 'Add amazing feature')
Push to the branch (git push origin feature/amazing-feature)
Open a Pull Request
📜 License
This project is licensed under the MIT License - see the LICENSE file for details.