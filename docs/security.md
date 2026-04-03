# Security

Spark includes multiple security layers to protect against prompt injection, unauthorised tool use, and configuration tampering.

## Prompt Inspection

The prompt inspection system scans user messages for security threats before they are sent to the LLM.

### Enabling

```yaml
prompt_inspection:
  enabled: true
  level: standard         # basic, standard, strict
  action: warn            # block, warn, sanitize, log_only
```

### Inspection Levels

| Level | Detection Methods |
|-------|-------------------|
| **basic** | Fast regex pattern matching only |
| **standard** | Patterns + suspicious keyword heuristics |
| **strict** | Patterns + keyword heuristics (with lower thresholds) |

### Threat Categories

| Category | Examples | Severity |
|----------|----------|----------|
| **Prompt injection** | "Ignore previous instructions", "Override system prompt", format string markers | High |
| **Jailbreak** | "DAN mode", "Enable developer mode", "Bypass safety filters" | High |
| **Code injection** | `exec()`, `eval()`, SQL injection patterns, shell injection | High |
| **PII** | Social Security numbers, credit card numbers, exposed API keys | Medium |
| **Suspicious keywords** | "jailbreak", "bypass", "unrestricted", "no limitations" | Low |

### Actions

| Action | Behaviour |
|--------|-----------|
| **block** | Reject the message entirely. The user sees a security warning. |
| **warn** | Flag the message but allow it through. A warning is logged. |
| **sanitize** | Attempt to remove the threatening content before processing. |
| **log_only** | Record the violation but take no action. |

For `warn` and `sanitize` modes, low-severity detections are logged without intervention while medium and high severity triggers the configured action.

### Violation Logging

All detected violations are recorded in the `prompt_inspection_violations` database table with:

- User GUID
- Violation type and severity
- Prompt snippet (first 200 characters)
- Detection method (level)
- Action taken
- Timestamp

## Tool Permissions

### Per-Conversation Permissions

When the AI first invokes a tool in a conversation, the user is prompted to approve or deny:

- **Allow Once** -- Permit this single invocation
- **Always Allow** -- Approve the tool and all tools in the same category
- **Deny** -- Block the tool and record the denial

Permissions are stored in the `conversation_tool_permissions` table and persist across sessions for the same conversation.

### Category-Based Approval

Approving a tool with "Always Allow" also approves all tools in the same category:

- Approving `web_search` also approves `web_fetch`
- Approving `read_file` also approves `list_directory`, `search_files`, etc.

### Auto-Approve Mode

To skip all permission prompts:

```yaml
tool_permissions:
  auto_approve: true
```

This is convenient for trusted environments but removes the permission safety net.

### Action Tool Permissions

Autonomous actions have their own tool permission table (`action_tool_permissions`) separate from conversation permissions.

## Settings Lock

The Settings page can be password-protected to prevent unauthorised configuration changes.

### Setting a Lock

1. Go to **Settings**
2. Click the lock icon
3. Enter a password

### How It Works

- The password hash (SHA-256) is stored in the OS keychain via the konfig secrets backend
- When locked, settings cannot be modified without entering the password
- The lock can be removed by entering the current password

## Authentication

### Web UI Authentication

Spark uses one-time authentication codes for local browser access:

1. On startup, an 8-character alphanumeric code is generated
2. The code is logged to the terminal
3. The browser auto-opens with the code in the URL for automatic login
4. Manual login is available at `/login` if auto-login fails

Authentication codes are validated via SHA-256 hash comparison. Sessions are managed with cookies.

### Session Management

- Sessions have a configurable timeout (default: 60 minutes)
- Session IDs are stored as HTTP-only cookies with `SameSite=Lax`
- The `/auto-login` endpoint is used for browser auto-open on startup

### User GUID

Each Spark installation generates a unique user GUID stored in the OS keychain. This GUID:

- Associates conversations, memories, and actions with the user
- Is generated once on first use and reused across all sessions
- Falls back to "default" if keychain access is unavailable

## Secret Management

All sensitive values (API keys, passwords, database credentials) should use `secret://` URIs:

```yaml
providers:
  anthropic:
    api_key: secret://anthropic_api_key
```

Secrets are stored in the OS keychain and resolved at runtime. The config file never contains plaintext secrets.

## SSL/TLS

Enable HTTPS for the web interface:

```yaml
interface:
  ssl:
    enabled: true
    cert_file: /path/to/cert.pem
    key_file: /path/to/key.pem
    auto_generate: true           # Generate a self-signed cert if none provided
```

With `auto_generate: true`, Spark generates a self-signed certificate on startup. This provides encryption but browsers will show a certificate warning.

## Data Security

- **Database:** SQLite with WAL journal mode and foreign keys enabled. For network databases, credentials are stored as secrets.
- **Filesystem access:** Restricted to configured `allowed_paths` only
- **Tool results:** Large results are automatically truncated
- **Heartbeat shutdown:** The server automatically shuts down when no browser is connected (configurable)
