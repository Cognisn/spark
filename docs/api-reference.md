# API Reference

Spark exposes a web API through FastAPI. All endpoints except authentication require a valid session cookie (`spark_session`).

Every POST, PUT, PATCH and DELETE must also send an `Origin` header naming the instance and the session's CSRF token in an `X-CSRF-Token` header (or a `csrf_token` form field); every request to `/stream/...` must send an `Origin` or `Referer` naming the instance. Spark's own pages do this for you: the token is in each page's `<meta name="csrf-token">`. A request that fails either check gets `403` with a `detail` message.

## Authentication

### GET /login

The sign-in page. Sets the `spark_login_csrf` cookie and the matching form field. If no sign-in code is live, a new one is shown in the terminal.

### POST /api/auth

Redeem a sign-in code and create a session. Each code works once, within `interface.login.code_ttl_seconds`.

**Form data:**
- `code` (string) -- The authentication code displayed in the terminal
- `csrf_token` (string) -- The token from the sign-in form

**Response:** Redirect to `/loading` with the session cookie set (`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS); `401` for a wrong, used or expired code; `429` while the address is locked out after repeated failures.

### GET /auto-login

Auto-login via URL query parameter. Used by browser auto-open on startup. The code is spent by its first use.

**Query params:**
- `code` (string) -- The authentication code

**Response:** Redirect to `/loading` with session cookie set, or redirect to `/login` on failure.

### POST /logout

End the session and clear its cookie. Needs the CSRF token.

**Response:** Redirect to `/login`.

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

**Response:** Array of matching conversation objects (the caller's own and those shared with them).

## Sharing (server mode)

Every route that takes a conversation id answers `404` to someone who is not a member of the conversation and `403` to a member whose role does not allow the request. Reading (history, tool activity, agents, info, export, members) is open to every member; sending to the owner and contributors; settings, tools, links, rename, model, deletion, sharing and transfer to the owner. In personal mode the routes below answer `404`.

### GET /conversations/api/shared

The conversations shared with the caller: `{"conversations": [...], "unseen": N}`. Each carries `role` (`contributor` or `viewer`), `owner_name`, `is_new` (1 until opened) and the caller's own `is_favourite`.

### GET /conversations/api/people?q=

Active users whose name or email contains `q` (at least two characters), at most ten, never the caller: `[{"id": "<user id>", "name": "...", "email": "..."}]`.

### GET /conversations/api/{conversation_id}/members

`{"role": "<caller's role>", "members": [...]}`, owner first; each member has `user_guid`, `role`, `name`, `initials`, `colour` (1 to 6) and `is_me`.

### POST /conversations/api/{conversation_id}/members

The owner shares with a person or changes a member's role. Body: `{"user_id": "<user id>", "role": "viewer"}` to add someone, or `{"user_guid": "...", "role": "contributor"}` to change a member's role. Answers `{"status": "added" | "changed" | "unchanged", "members": [...]}`.

### DELETE /conversations/api/{conversation_id}/members/{user_guid}

The owner removes a person, or a member leaves (their own guid). Takes effect at once.

### POST /conversations/api/{conversation_id}/transfer

The owner hands the conversation to a member (`{"user_guid": "..."}`) or to a person added in the transfer (`{"user_id": "<user id>"}`); the previous owner becomes a contributor.

### GET /stream/conversation?conversation_id=

Server-sent events for everyone viewing a conversation, in both modes (personal mode on the app's own process): `ready` (`me`, the caller's guid, and the running `turn`, if any: `user_guid`, `name`, `stream_id`, `started_at`), `turn_started` (`user_guid`, `name`, `stream_id`), `delta` (`seq`, `text`), `tool` (`phase` `start`, `end` or `approval`, `tool_name`), `turn_finished` (`status`, `user_guid`, `stream_id`), `members_changed`, `member_removed`; the stream ends with `removed` when the caller loses access and `deleted` when the conversation is deleted.

When the running turn is the caller's own (they came back to the conversation, or it runs in another tab), these follow to the caller's pages only:

- `turn_state`: `stream_id`, `started_at`, `activity` (`kind` `thinking`, `tool` with `tool_name`, or `compaction`), `agents` (`agent_id`, `agent_name`) and `prompts` (the held prompts, as `prompt` gives them), then `turn_text` (`stream_id`, `text`: the reply since the last tool started);
- `prompt`: a held tool permission prompt (`kind` `permission`, `request_id`, `stream_id`, `tool_name`, `params`) or agent model approval (`kind` `agent_model`, with `agent_name`, `task`, `suggested_model`, `available_models`, `justification`); `prompt_closed` (`request_id`) when it is answered or ends;
- `agent` (`phase` `start` or `end`, `agent_id`, `agent_name`) and `compaction` (`phase`).

A held prompt waits 10 minutes while no page of the sender's has the conversation open, and 120 seconds from the moment one is; then a tool permission is denied and an agent model approval takes the suggested model. A turn runs to its own end however long no page is open.

### GET /stream/running

The conversations where a turn of the caller's is running: `{"conversations": [12]}` (the working marker of the Conversations page and the projects tree).

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

### POST /chat/{conversation_id}/api/send

Send a message without streaming. Body: `{"message": "...", "attachments": [12, 13]}`; `attachments` is optional, follows the same rules as on `GET /stream/chat`, and may stand without a message. In a shared conversation a read-only member is answered `403`, a sender who may not use the conversation's model `403`, and a send while another participant's turn runs `409` with `{"error": "Waiting for <name>'s turn to finish", "holder": {...}}`; `GET /stream/chat` answers the same way before it streams. In personal mode a send while an earlier one in the same conversation is still being answered is refused with `409` too.

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
- `attachments` (string, optional) -- Comma-separated ids of the user's own files (uploaded first with `POST /files/api`), at most 20. Another user's id, or one that does not exist, answers 404 and nothing runs; ids that are not numbers answer 400. The message the model receives ends with a note naming each file by id and name (never its content):

  ```
  [Attached files: call read_my_file with the file id to read each one]
  - file 12: report.pdf
  ```

  Messages stored by 0.3.0rc43 and earlier open the note with `[Attached files: read them with the read_my_file tool]`; the chat page reads both into chips.

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

### POST /chat/permission/respond

Respond to a tool permission request.

**Body:**
```json
{
  "request_id": "abc12345",
  "decision": "allowed",
  "stream_id": "<the turn's stream id>",
  "conversation_id": 12
}
```

`decision` is one of: `allowed` (always allow), `once` (allow this time), or `denied`. `stream_id` and `conversation_id` let a server with several replicas pass the answer to the replica running the turn; there, an id that is not the caller's running turn answers `404`. `POST /chat/agent/model-approve` (`request_id`, `model_id`) and `POST /chat/agent/cancel` (`agent_id`) take the same two fields.

### POST /stream/cancel

Stop a running turn and every agent of it, from any of the sender's pages: `{"stream_id": "...", "conversation_id": 12}`. On a server another user's turn, or one that has ended, answers `404`.

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

Test an MCP server connection without saving it.

### POST /settings/mcp/api/toggle

Enable or disable a server: `{"name": "my-server", "enabled": false}`.

### GET /settings/mcp/api/{server_name}/config

One server's configuration, for editing.

### GET /settings/mcp/api/{server_name}/tools

The tools a server offers.

### POST /settings/mcp/api/{server_name}/tools/refresh

Re-list one server's tools, reconnecting it first if it has dropped. Answers `{"name", "status": "ok", "tools", "added", "removed", "changed"}`, the three change lists being tool names. A server that fails or times out keeps its old tools and answers 502 with `"status": "failed"` and an `error`; an unknown server answers 404. On a server this needs `spark.settings.instance` and is audited as `spark.mcp.tools.refreshed`.

### GET /settings/mcp/api/roles

The roles a server can be granted to (server mode's editor).

### DELETE /settings/mcp/api/{server_name}

Delete an MCP server configuration.

### Server mode

On a server these endpoints need `spark.settings.instance` (401 signed out, 403 without it) and work on the `mcp.servers` instance setting, never `config.yaml`:

- `transport` must be `http` or `sse`; a stdio server answers 400 with the reason.
- `name` is lower-case letters, digits, `-` and `_`.
- `roles` lists the roles the server is granted to (see `/api/roles`); an unknown role answers 400.
- `auth_token`, `basic_password` and `custom_headers` are written to the secrets store; an empty value keeps the stored one, and `clear_secrets` (a list of those field names) removes them. Responses never contain a credential: `config` and `list` report `secrets_set` instead.
- Toggling or deleting a server that does not exist answers 404; deleting a server removes its credentials.

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

### GET /health

Liveness probe for load balancers and container health checks. Answers 200 whenever the
process serves, with no dependency and no sign-in (personal and server mode).

```json
{"status": "ok", "version": "0.3.0"}
```

### GET /ready

Readiness probe. Answers 200 only when the database answers, the schema is at both Alembic
heads (Spark's and warden's) and the secrets store is readable; otherwise 503. It needs no
sign-in, and its body names each check as `ok` or `failed` and never carries a secret, URL,
host name or stack trace.

```json
{"status": "ready", "version": "0.3.0", "checks": {"database": "ok", "schema": "ok", "secrets": "ok"}}
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
