# Tests

Index of the tests in this folder. Keep it current as tests are added, changed, or removed.

| Test | Covers |
| --- | --- |
| `test_agent_executor.py` | Sub-agent execution engine (orchestrator-workers and chain modes) |
| `test_agents_db.py` | Agent record persistence in the database |
| `test_application.py` | Application bootstrap and konfig `AppContext` lifecycle |
| `test_auth.py` | Settings-lock password protection and authentication |
| `test_cancellation.py` | Stream and per-agent cancellation (`/stream/cancel`, cancel tokens) |
| `test_chat_endpoints.py` | Chat API endpoints and message handling |
| `test_comprehensive.py` | Broad end-to-end integration coverage across subsystems |
| `test_conversation_links.py` | Linking context between related conversations |
| `test_conversation_manager.py` | Core conversation manager orchestration |
| `test_daemon.py` | Background daemon and autonomous action execution |
| `test_database.py` | Database backends (SQLite/MySQL/PostgreSQL/MSSQL) and schema |
| `test_index.py` | Memory vector index and semantic search |
| `test_llm.py` | LLM provider abstraction and provider selection |
| `test_main_menu.py` | Web navigation and main menu |
| `test_mcp.py` | MCP server integration and tool routing |
| `test_new_endpoints.py` | Newer web endpoints |
| `test_safety.py` | Prompt-injection detection and tool permissions |
| `test_server.py` | FastAPI server and SSE streaming |
| `test_session.py` | Web session management |
| `test_settings_helpers.py` | Settings helper utilities |
| `test_system_command.py` | System-command tool execution and approval |
| `test_tools.py` | Built-in tool definitions and execution |
| `test_version.py` | Version single-sourcing from `_version.txt` |
