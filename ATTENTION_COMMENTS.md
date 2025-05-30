# A2A Edge Node Redesign for Subscriber-Based Bus Architecture

## Overview

This document outlines the redesigned architecture for the A2A (Agent-to-Agent) edge node, following the subscriber-based patterns established in the tg_edge_handler and bus_adapterV2 implementations. This approach aligns with the existing bus architecture and provides a consistent integration pattern for A2A protocol support.

## Design Principles

1. **Subscriber-Driven**: Use Redis stream subscriptions rather than RPC-style calls
2. **Dynamic Registration**: Support runtime registration of A2A agents
3. **Consistent Addressing**: Maintain the AG1:edge:a2a:{agent_name}:response pattern
4. **Adapter Pattern**: Leverage BusAdapterV2 for subscription management
5. **Stateful Connections**: Maintain persistent connections to A2A endpoints

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│  Agent System   │◄───►│  A2A Edge Node  │◄───►│  External A2A   │
│  (Redis Bus)    │     │  (Subscriber)   │     │  Agents         │
│                 │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

## Components

### 1. A2A Edge Handler

Similar to the tg_edge_handler, this component:
- Subscribes to registration and message channels
- Manages A2A agent registrations
- Routes messages between the bus and A2A endpoints
- Handles streaming responses

### 2. A2A Bus Adapter

Based on BusAdapterV2, this component:
- Provides a clean API for subscriptions and publishing
- Manages dynamic subscription to response channels
- Handles correlation between requests and responses
- Supports introspection of current connections

### 3. A2A Session Manager

Maintains persistent connections to A2A endpoints:
- Manages authentication and token refresh
- Handles connection pooling and reuse
- Supports both regular and streaming connections
- Tracks task state for long-running operations

## Channel Naming Convention

- **Registration Channel**: `AG1:a2a:register`
- **Edge Node Inbox**: `AG1:edge:a2a:inbox`
- **Response Channels**: `AG1:edge:a2a:{agent_name}:response`
- **Task Status Channels**: `AG1:edge:a2a:{agent_name}:status:{task_id}`

## Message Flow

### Agent Registration

```
1. Local Agent ──► AG1:a2a:register ──► A2A Edge Handler
   (Envelope with agent_name, a2a_endpoint, auth_info)

2. A2A Edge Handler ──► Stores registration
   (Maps local agent to A2A endpoint)

3. A2A Edge Handler ──► AG1:edge:a2a:{agent_name}:response ──► Local Agent
   (Registration confirmation)
```

### Message Flow (Outgoing)

```
1. Local Agent ──► AG1:edge:a2a:inbox ──► A2A Edge Handler
   (Envelope with method, params)

2. A2A Edge Handler ──► External A2A Agent
   (HTTP request with JSON-RPC payload)

3. External A2A Agent ──► A2A Edge Handler
   (HTTP response with JSON-RPC result)

4. A2A Edge Handler ──► AG1:edge:a2a:{agent_name}:response ──► Local Agent
   (Envelope with result)
```

### Streaming Response Flow

```
1. A2A Edge Handler ──► Creates subscription to SSE stream
   (Persistent connection to A2A endpoint)

2. External A2A Agent ──► A2A Edge Handler
   (SSE stream with incremental updates)

3. A2A Edge Handler ──► AG1:edge:a2a:{agent_name}:response ──► Local Agent
   (Multiple envelopes with incremental results)
```

## Envelope Structure

### Registration Envelope

```json
{
  "envelope_type": "register",
  "agent_name": "MyLocalAgent",
  "content": {
    "a2a_endpoint": "https://example.com/a2a",
    "auth_type": "Bearer",
    "auth_key": "your_auth_token"
  },
  "timestamp": "2025-05-22T19:07:21Z"
}
```

### Request Envelope

```json
{
  "role": "user",
  "content": {
    "method": "tasks/create",
    "params": {
      "messages": [
        {
          "role": "user",
          "parts": [
            {
              "text": "Hello from the bus architecture!"
            }
          ]
        }
      ]
    }
  },
  "user_id": "local_user",
  "correlation_id": "abc123",
  "agent_name": "MyLocalAgent",
  "reply_to": "AG1:edge:a2a:MyLocalAgent:response"
}
```

### Response Envelope

```json
{
  "role": "agent",
  "content": {
    "result": {
      "task": {
        "id": "task_123",
        "state": "running"
      }
    }
  },
  "user_id": "a2a_bridge",
  "correlation_id": "abc123",
  "agent_name": "a2a_bridge",
  "reply_to": null,
  "meta": {
    "source_envelope_id": "original_envelope_id"
  }
}
```

## Implementation Strategy

### 1. Core Handler Functions

```python
async def handle_registration(env, redis):
    """Handle agent registration with the A2A edge"""
    # Extract registration info
    # Store in agent registry
    # Send confirmation
    
async def handle_a2a_request(env, redis):
    """Process A2A request and forward to A2A endpoint"""
    # Extract method, params
    # Get agent info from registry
    # Call A2A endpoint
    # Publish response to reply_to channel
    
async def handle_streaming_response(env, redis):
    """Handle streaming responses from A2A endpoints"""
    # Set up SSE connection
    # Process stream events
    # Publish updates to reply_to channel
```

### 2. Bus Adapter Setup

```python
# Create Redis client
redis = aioredis.from_url(build_redis_url())

# Create bus adapter
adapter = BusAdapterV2(
    agent_id="a2a_edge",
    core_handler=handle_a2a_request,
    redis_client=redis,
    patterns=["AG1:edge:a2a:inbox"],
    group="a2a_edge_group"
)

# Add registration handler
await adapter.add_subscription("AG1:a2a:register", handle_registration)

# Start the adapter
await adapter.start()
```

### 3. A2A Client Functions

```python
async def call_a2a_endpoint(endpoint, method, params, auth_type, auth_key):
    """Call an A2A endpoint with JSON-RPC"""
    # Prepare JSON-RPC request
    # Set up authentication
    # Make HTTP request
    # Return JSON-RPC response
    
async def setup_streaming_connection(endpoint, method, params, auth_type, auth_key):
    """Set up streaming connection to A2A endpoint"""
    # Prepare JSON-RPC request
    # Set up authentication
    # Create SSE connection
    # Return stream handler
```

## Key Differences from Original Design

1. **Subscription-Based**: Uses Redis stream subscriptions rather than direct RPC calls
2. **BusAdapter Integration**: Leverages BusAdapterV2 for subscription management
3. **Dynamic Registration**: Supports runtime registration of A2A agents
4. **Persistent Connections**: Maintains connections to A2A endpoints for streaming
5. **Consistent Addressing**: Uses the same channel naming conventions as other edge nodes

## Next Steps

1. Implement the A2A edge handler with BusAdapterV2 integration
2. Add support for A2A agent registration
3. Implement A2A endpoint communication with JSON-RPC
4. Add support for streaming responses via SSE
5. Create test scripts and documentation
