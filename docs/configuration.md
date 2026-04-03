# Configuration

Spark uses [cognisn-konfig](https://pypi.org/project/cognisn-konfig/) for settings, secrets, and logging. Configuration is loaded from a YAML file and can be overridden with environment variables.

## Config File Location

The config file is created automatically on first run at the platform-specific config directory.

| Platform | Config Path |
|----------|-------------|
| macOS | `~/Library/Application Support/spark/config.yaml` |
| Linux | `~/.config/spark/config.yaml` |
| Windows | `%APPDATA%\spark\config.yaml` |

## Data and Log Locations

| Platform | Data Directory | Log Directory |
|----------|---------------|---------------|
| macOS | `~/Library/Application Support/spark/` | `~/Library/Logs/spark/` |
| Linux | `~/.local/share/spark/` | `~/.local/state/spark/logs/` |
| Windows | `%APPDATA%\spark\` | `%LOCALAPPDATA%\spark\logs\` |

The SQLite database (`spark.db`) is stored in the data directory by default.

## Environment Variables

Any setting can be overridden using environment variables with the `SPARK__` prefix. Nested keys use double underscores as separators:

```bash
# Override database host
export SPARK__DATABASE__HOST=localhost

# Override logging level
export SPARK__LOGGING__LEVEL=DEBUG

# Override interface host
export SPARK__INTERFACE__HOST=0.0.0.0
```

## Secrets Management

API keys and passwords can be stored securely using `secret://` URIs in the config file. The konfig secrets backend uses the OS keychain:

- **macOS:** Keychain Access
- **Windows:** Windows Credential Locker
- **Linux:** Secret Service (GNOME Keyring / KDE Wallet)

```yaml
providers:
  anthropic:
    enabled: true
    api_key: secret://anthropic_api_key
```

When Spark encounters a `secret://` URI, it resolves the value from the keychain. Secrets entered via the Settings UI are automatically stored in the keychain.

## Complete Configuration Reference

### Logging

```yaml
logging:
  level: INFO                    # DEBUG, INFO, WARNING, ERROR, CRITICAL
  format: text                   # text or json
  retention_runs: 10             # Number of historical log directories to keep
  max_file_size_mb: 50           # Rotate log file at this size
  max_files_per_run: 3           # Rotated files per run directory
  console_output: auto           # auto, stderr, or none
```

Spark creates a new log directory for each run inside the platform log directory. Old run directories are pruned based on `retention_runs`.

### Database

```yaml
database:
  type: sqlite                   # sqlite, mysql, postgresql, mssql
  path: spark.db                 # SQLite file path (relative to data dir)
  # host: localhost              # For mysql/postgresql/mssql
  # port: 5432
  # name: spark
  # user: spark
  # password: secret://db_password
```

SQLite is the default and requires no additional configuration. For other databases, install the appropriate extra (see [Getting Started](getting-started.md)).

### Web Interface

```yaml
interface:
  host: 127.0.0.1               # Bind address (use 0.0.0.0 for network access)
  ssl:
    enabled: false
    cert_file: /path/to/cert.pem
    key_file: /path/to/key.pem
    auto_generate: true          # Generate self-signed cert if none provided
  session_timeout_minutes: 60
  browser_heartbeat:
    enabled: true
    interval_seconds: 30
    miss_threshold: 3            # Shutdown after N missed heartbeats
```

The port is randomly chosen on each startup. The browser heartbeat monitor shuts down the server when no browser tabs are connected (after `miss_threshold` consecutive missed heartbeats at `interval_seconds` intervals).

### LLM Providers

```yaml
providers:
  anthropic:
    enabled: false
    api_key: secret://anthropic_api_key

  aws_bedrock:
    enabled: false
    region: us-east-1
    profile: default             # AWS profile name

  ollama:
    enabled: false
    base_url: http://localhost:11434

  google_gemini:
    enabled: false
    api_key: secret://google_gemini_api_key

  xai:
    enabled: false
    api_key: secret://xai_api_key
```

See [Providers](providers.md) for full details on each provider.

### Conversation Settings

```yaml
conversation:
  rollup_threshold: 0.3           # Compact at 30% of context window
  rollup_summary_ratio: 0.3       # Compress to 30% of original size
  emergency_rollup_threshold: 0.95 # Force compaction at 95%
  max_tool_iterations: 25         # Max tool use loops per message
  max_tool_selections: 30         # Max tools to send to model
  max_tool_result_tokens: 4000    # Max tokens per tool result
  global_instructions: |          # Prepended to all conversations
    You are a helpful AI assistant.
```

### Context Limits

Override or define context window sizes for specific models:

```yaml
context_limits:
  claude-sonnet-4-20250514:
    context_window: 200000
    max_output: 16384
  llama3:
    context_window: 8192
    max_output: 4096
```

Resolution priority:
1. Exact match in config overrides
2. Partial match (model ID contains the pattern)
3. Built-in defaults for known model families
4. Global default (8192 context / 4096 output)

### Prompt Inspection

```yaml
prompt_inspection:
  enabled: false
  level: standard                # basic, standard, strict
  action: warn                   # block, warn, sanitize, log_only
```

See [Security](security.md) for details.

### Tool Permissions

```yaml
tool_permissions:
  auto_approve: false            # If true, skip permission prompts for all tools
```

### Embedded Tools

```yaml
embedded_tools:
  filesystem:
    enabled: true
    mode: read                   # read or read_write
    allowed_paths: []            # List of accessible directory paths

  documents:
    enabled: true
    mode: read
    max_file_size_mb: 50

  archives:
    enabled: true
    mode: list                   # list or extract

  web:
    enabled: true
    search_engine: duckduckgo    # duckduckgo, brave, google, bing, searxng
    brave_api_key: ""
    google_api_key: ""
    bing_api_key: ""
    searxng_url: ""
```

See [Tools](tools.md) and [Web Search](web-search.md) for details.

### Default Model

```yaml
default_model:
  model_id: gemini-2.5-flash    # Pre-selected model for new conversations
  mode: default                  # default = pre-selected, mandatory = locked
```

### MCP Servers

```yaml
mcp:
  servers:
    - name: example-server
      transport: stdio           # stdio, http, sse
      command: npx
      args: ["-y", "@example/mcp-server"]
      env:
        API_KEY: secret://example_api_key
      auth_type: none            # none, bearer, api_key, basic, custom
      timeout: 30
      ssl_verify: true
```

See [MCP Integration](mcp-integration.md) for full details.

### Autonomous Actions

```yaml
autonomous_actions:
  enabled: false
  max_concurrent: 3

daemon:
  enabled: false
  pid_file: spark_daemon.pid
  heartbeat_interval: 30
```

See [Autonomous Actions](autonomous-actions.md) for details.

### Token Management

```yaml
token_management:
  enabled: false
  limit: 1000000               # Max tokens per rolling window
  window_hours: 24             # Rolling window duration
```
