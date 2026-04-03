# Development

This guide covers contributing to Spark, project structure, testing, and releasing.

## Prerequisites

- Python 3.12 or later
- Git

## Setting Up a Development Environment

```bash
# Clone the repository
git clone https://github.com/Cognisn/spark.git
cd spark

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # macOS/Linux
# .venv\Scripts\activate    # Windows

# Install in development mode with dev dependencies
pip install -e ".[dev]"
```

## Project Structure

```
spark/
├── src/spark/
│   ├── __init__.py
│   ├── _version.txt                  # Version file (read by hatchling)
│   ├── launch.py                     # Entry point
│   ├── core/
│   │   ├── application.py            # Bootstrap and lifecycle
│   │   ├── conversation_manager.py   # Central orchestration
│   │   ├── context_compaction.py     # LLM-driven summarisation
│   │   ├── updater.py                # GitHub update checker
│   │   └── user_guid.py              # Persistent user identifier
│   ├── web/
│   │   ├── server.py                 # FastAPI app creation and startup
│   │   ├── auth.py                   # One-time auth code manager
│   │   ├── session.py                # Session management
│   │   ├── ssl_utils.py              # Self-signed certificate generation
│   │   ├── endpoints/
│   │   │   ├── auth.py               # Login and auto-login
│   │   │   ├── main_menu.py          # Dashboard / home
│   │   │   ├── conversations.py      # Conversation CRUD
│   │   │   ├── chat.py               # Chat page and message API
│   │   │   ├── streaming.py          # SSE streaming endpoint
│   │   │   ├── settings.py           # Settings page and API
│   │   │   ├── memories.py           # Memory management
│   │   │   ├── actions.py            # Autonomous action management
│   │   │   ├── mcp_servers.py        # MCP server configuration
│   │   │   └── help.py               # Built-in user guide
│   │   ├── templates/                # Jinja2 HTML templates
│   │   └── static/
│   │       ├── css/                  # base.css, cognisn.css
│   │       ├── js/                   # ui.js, chat.js, sse-client.js
│   │       └── fonts/                # Syne, DM Sans, Nesobrite
│   ├── llm/
│   │   ├── base.py                   # LLMService abstract base
│   │   ├── manager.py                # Multi-provider router
│   │   ├── context_limits.py         # Context window resolver
│   │   ├── anthropic_direct.py       # Anthropic SDK provider
│   │   ├── bedrock.py                # AWS Bedrock provider
│   │   ├── ollama.py                 # Ollama local provider
│   │   ├── google_gemini.py          # Google Gemini provider
│   │   └── xai.py                    # X.AI / Grok provider
│   ├── tools/
│   │   ├── registry.py               # Tool assembly and dispatch
│   │   ├── filesystem.py             # File read/write/search tools
│   │   ├── documents.py              # Word/Excel/PDF/PowerPoint
│   │   ├── archives.py               # ZIP/TAR listing and extraction
│   │   ├── web.py                    # Web search and fetch
│   │   ├── memory_tools.py           # Memory CRUD tools
│   │   └── datetime_tool.py          # Current date/time
│   ├── mcp_integration/
│   │   ├── manager.py                # MCP server connection manager
│   │   └── tool_selector.py          # Intelligent tool selection
│   ├── safety/
│   │   ├── inspector.py              # Prompt inspection engine
│   │   └── patterns.py               # Attack pattern definitions
│   ├── database/
│   │   ├── schema.py                 # Table creation and migrations
│   │   ├── backends.py               # SQLite/MySQL/PostgreSQL/MSSQL
│   │   ├── connection.py             # Database connection wrapper
│   │   ├── conversations.py          # Conversation CRUD
│   │   ├── messages.py               # Message CRUD
│   │   ├── memories.py               # Memory CRUD
│   │   ├── autonomous_actions.py     # Action CRUD
│   │   ├── conversation_links.py     # Conversation linking
│   │   ├── mcp_ops.py                # MCP transaction recording
│   │   ├── tool_permissions.py       # Tool permission CRUD
│   │   ├── usage.py                  # Token usage tracking
│   │   ├── files.py                  # Conversation file attachments
│   │   └── context_index.py          # Vector index storage
│   ├── index/
│   │   ├── embeddings.py             # Sentence-transformer model
│   │   ├── memory_index.py           # Persistent memory index
│   │   └── vector_index.py           # Conversation vector index
│   ├── scheduler/
│   │   ├── runner.py                 # APScheduler action scheduler
│   │   ├── executor.py               # Single action executor
│   │   └── creation_tools.py         # AI-assisted action creation
│   ├── daemon/
│   │   ├── tray.py                   # System tray daemon
│   │   ├── manager.py                # Daemon lifecycle management
│   │   └── app.py                    # Daemon entry point
│   └── resources/
│       ├── config.yaml.template      # Default configuration template
│       └── tool_docs/                # Markdown tool documentation
│           ├── _index.md
│           ├── read_file.md
│           ├── web_search.md
│           └── ...
├── tests/
├── docs/
├── pyproject.toml
├── CLAUDE.md
├── README.md
├── LICENSE
└── CHANGELOG.md
```

## Code Style

The project uses:

- **black** for code formatting (line length: 100, target: Python 3.12)
- **isort** for import sorting (profile: black)
- **mypy** for type checking (lenient mode -- `ignore_missing_imports: true`)

```bash
# Format code
black src/ tests/

# Sort imports
isort src/ tests/

# Type check
mypy src/spark/
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=spark --cov-report=term-missing

# Run a specific test file
pytest tests/test_conversation_manager.py

# Run with verbose output
pytest -v
```

Test configuration is in `pyproject.toml`:

- Test paths: `tests/`
- Async mode: `auto` (via pytest-asyncio)
- Timeout: 60 seconds per test
- Output: verbose with short tracebacks

## Adding a New LLM Provider

1. Create `src/spark/llm/your_provider.py`
2. Implement the `LLMService` abstract base class:
   - `get_provider_name()` -- Human-readable name
   - `get_access_info()` -- Authentication description
   - `list_available_models()` -- Available models list
   - `set_model()` -- Set active model
   - `invoke_model()` -- Send messages, return normalised response
   - `supports_streaming()` -- Whether streaming is supported
   - `count_tokens()` -- Token estimation
3. Register in `server.py::_init_providers()` with settings loading
4. Add config section in `config.yaml.template`
5. Add context limits in `context_limits.py`

## Adding a New Built-in Tool

1. Create `src/spark/tools/your_tool.py`
2. Define tool schema as a list of dicts with `name`, `description`, and `inputSchema`
3. Implement `get_tools()` and `execute()` functions
4. Register in `registry.py::get_builtin_tools()` and `execute_builtin_tool()`
5. Add tool documentation in `resources/tool_docs/your_tool.md`
6. Add the tool name to the appropriate category in `conversation_manager.py::_TOOL_CATEGORIES`

## Building

```bash
# Install build tools
pip install build twine

# Build source distribution and wheel
python -m build

# Verify the build
twine check dist/*
```

## Releasing

1. Update the version in `src/spark/_version.txt`
2. Update `CHANGELOG.md`
3. Build: `python -m build`
4. Upload to PyPI: `twine upload dist/*`
5. Create a GitHub release with the version tag
6. Optionally build PyApp binaries for each platform

## Dependencies

### Core Runtime

| Package | Purpose |
|---------|---------|
| cognisn-konfig | Settings, secrets, logging |
| fastapi, uvicorn | Web server |
| jinja2 | Template rendering |
| sse-starlette | SSE streaming |
| httpx, aiohttp | HTTP clients |
| pyyaml | Config file parsing |
| tiktoken | Token counting |
| cryptography | SSL certificate generation |

### LLM Providers

| Package | Provider |
|---------|----------|
| anthropic | Anthropic Direct API |
| boto3, botocore | AWS Bedrock |
| google-genai | Google Gemini |
| ollama | Ollama local models |
| openai | X.AI (OpenAI-compatible) |

### Tools and Features

| Package | Purpose |
|---------|---------|
| mcp | Model Context Protocol |
| python-docx, openpyxl, python-pptx, pdfplumber | Document reading |
| sentence-transformers, numpy | Embeddings and vector search |
| beautifulsoup4, html2text | Web content parsing |
| APScheduler | Action scheduling |
| pystray, Pillow | System tray daemon |

### Optional Database Drivers

| Package | Database |
|---------|----------|
| mysql-connector-python | MySQL / MariaDB |
| psycopg2-binary | PostgreSQL |
| pyodbc | Microsoft SQL Server |
