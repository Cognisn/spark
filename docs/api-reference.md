# API Reference

Spark exposes a web API through FastAPI. All endpoints except authentication require a valid session cookie (`spark_session`).

## Authentication

### POST /api/auth

Validate an auth code and create a session.

**Form data:**
- `code` (string) -- The authentication code displayed in the terminal

**Response:** Redirect to `/loading` with session cookie set.

### GET /auto-login

Auto-login via URL query parameter. Used by browser auto-open on startup.

**Query params:**
- `code` (string) -- The authentication code

**Response:** Redirect to `/loading` with session cookie set, or redirect to `/login` on failure.

## Conversations

### GET /conversations/api/list

List all active conversations.

**Response:**
```json
[
  {
    "id": 1,
    "name": "My Conversation",
    "model_id": "claude-sonnet-4-20250514",
    "created_at": "2026-03-15T10:00:00",
    "last_updated": "2026-03-15T12:00:00",
    "total_tokens": 5200,
    "is_favourite": 0
  }
]
```

### POST /conversations/api/create

Create a new conversation.

**Body:**
```json
{
  "name": "My Conversation",
  "model_id": "claude-sonnet-4-20250514",
  "instructions": "Optional custom system prompt",
  "web_search_enabled": false
}
```

**Response:**
```json
{
  "id": 1,
  "name": "My Conversation"
}
```

### DELETE /conversations/api/{conversation_id}

Soft-delete a conversation.

**Response:**
```json
{"status": "ok"}
```

### GET /conversations/api/search

Search conversations by keyword.

**Query params:**
- `q` (string) -- Search query

**Response:** Array of matching conversation objects.

## Chat

### GET /chat/{conversation_id}/api/history

Get message history for a conversation.

**Response:**
```json
[
  {
    "id": 1,
    "role": "user",
    "content": "Hello",
    "token_count": 5,
    "timestamp": "2026-03-15T10:00:00"
  },
  {
    "id": 2,
    "role": "assistant",
    "content": "Hi! How can I help?",
    "token_count": 12,
    "timestamp": "2026-03-15T10:00:01"
  }
]
```

### GET /chat/{conversation_id}/api/export

Export a conversation in various formats.

**Query params:**
- `format` (string) -- `markdown`, `html`, `csv`, or `json`

**Response:** File download in the requested format.

## Streaming

### GET /stream/chat

SSE endpoint for streaming chat responses.

**Query params:**
- `message` (string) -- The user message
- `conversation_id` (integer) -- The conversation ID

**SSE Events:**

| Event | Data | Description |
|-------|------|-------------|
| `status` | `{"status": "processing"}` | Request received |
| `text` | `{"text": "..."}` | Streamed text token |
| `tool_call` | `{"tool_use_id": "...", "tool_name": "...", "params": {...}}` | Tool invocation |
| `tool_result` | `{"tool_use_id": "...", "tool_name": "...", "result": "...", "status": "success"}` | Tool result |
| `permission_request` | `{"request_id": "...", "tool_name": "...", "params": {...}}` | Tool permission prompt |
| `compaction_start` | `{"tokens": N, "threshold": N}` | Context compaction beginning |
| `compaction_complete` | `{"original_tokens": N, "new_tokens": N, "messages_rolled_up": N}` | Compaction finished |
| `complete` | `{"content": "...", "usage": {...}, "tool_calls": [...]}` | Response complete |
| `error` | `{"message": "..."}` | Error occurred |

### POST /stream/permission/respond

Respond to a tool permission request.

**Body:**
```json
{
  "request_id": "abc12345",
  "decision": "allowed"
}
```

`decision` is one of: `allowed` (always allow), `once` (allow this time), or `denied`.

## Settings

### GET /settings

Render the settings page (HTML).

### POST /settings/api/save

Save settings changes.

**Body:** JSON object with the settings to update.

### POST /settings/api/lock

Set or update the settings lock password.

**Body:**
```json
{
  "password": "new-password",
  "current_password": "old-password"
}
```

### POST /settings/api/unlock

Verify the lock password.

**Body:**
```json
{
  "password": "the-password"
}
```

## MCP Servers

### GET /settings/mcp/api/list

List all configured MCP servers with connection status.

**Response:**
```json
[
  {
    "name": "my-server",
    "transport": "stdio",
    "enabled": true,
    "connected": true,
    "tool_count": 5
  }
]
```

### POST /settings/mcp/api/save

Save a new or updated MCP server configuration.

**Body:**
```json
{
  "name": "my-server",
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@example/mcp-server"],
  "env": {},
  "auth_type": "none",
  "timeout": 30,
  "ssl_verify": true,
  "enabled": true
}
```

### POST /settings/mcp/api/test

Test an MCP server connection.

### POST /settings/mcp/api/connect/{server_name}

Connect to a specific MCP server.

### POST /settings/mcp/api/disconnect/{server_name}

Disconnect from a specific MCP server.

### DELETE /settings/mcp/api/{server_name}

Delete an MCP server configuration.

## Memories

### GET /memories/api/list

List all memories.

**Query params:**
- `category` (string, optional) -- Filter by category
- `limit` (integer, default: 100) -- Maximum results

**Response:**
```json
{
  "memories": [
    {
      "id": 1,
      "content": "User prefers dark mode",
      "category": "preferences",
      "importance": 0.7,
      "created_at": "2026-03-15T10:00:00"
    }
  ],
  "total": 1
}
```

### GET /memories/api/stats

Get memory statistics.

**Response:**
```json
{
  "total": 25,
  "by_category": {
    "preferences": 5,
    "facts": 12,
    "projects": 8
  },
  "avg_importance": 0.55
}
```

### POST /memories/api/import

Import memories from a JSON file upload.

### GET /memories/api/export

Export all memories as JSON.

### DELETE /memories/api/{memory_id}

Delete a specific memory.

### DELETE /memories/api/all

Delete all memories (requires confirmation).

## Autonomous Actions

### GET /actions/api/list

List all actions (enabled and disabled).

### POST /actions/api/create

Create a new autonomous action.

**Body:**
```json
{
  "name": "Daily Summary",
  "description": "Generate a daily briefing",
  "prompt": "Summarise the latest developments...",
  "model_id": "claude-sonnet-4-20250514",
  "schedule_type": "recurring",
  "schedule_config": "{\"cron\": \"0 8 * * 1-5\"}",
  "context_mode": "fresh",
  "max_failures": 3,
  "max_tokens": 8192
}
```

### PUT /actions/api/{action_id}

Update an existing action.

### DELETE /actions/api/{action_id}

Delete an action.

### POST /actions/api/{action_id}/toggle

Enable or disable an action.

### GET /actions/api/{action_id}/runs

Get the run history for an action.

## Help

### GET /help

Render the built-in user guide (HTML).

### GET /help/api/search

Search help topics.

**Query params:**
- `q` (string) -- Search query

**Response:** Array of matching help topic objects.

## System

### GET /loading/api/status

Get the server initialisation status (available without authentication).

**Response:**
```json
{
  "ready": true,
  "error": false,
  "stage": "Ready"
}
```

### POST /api/heartbeat

Browser heartbeat to keep the server alive.

### GET /api/update-info

Get update availability information.

**Response:**
```json
{
  "available": true,
  "current_version": "0.1.0",
  "latest_version": "0.2.0",
  "release_url": "https://github.com/Cognisn/spark/releases/tag/v0.2.0",
  "install_method": "pip"
}
```

### POST /api/update

Apply an available update.
