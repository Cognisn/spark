# Changelog

All notable changes to Spark will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-16

### Added
- **System Command Tool** — Execute shell commands (git, docker, aws, curl, etc.) with OS-aware execution, blocked command list, and configurable approval prompts
- **Agent Spawning** — LLM can spawn independent sub-agents via `spawn_agent` tool with dedicated Agents tab in resizable sidecar panel; supports orchestrator-workers and chain modes; model auto-selection with user approval
- **Email Tool** — Send and draft emails via SMTP with HTML/plain text, to/cc/bcc, file attachments; SMTP test connection button; passwords stored in OS keychain
- **Document Creation** — Create Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and PDF documents with advanced formatting (headings, tables, lists, images, styles)
- **Provider Setup Guides** — In-app setup guides for all LLM providers (Anthropic, AWS Bedrock, Ollama, Google Gemini, X.AI) with step-by-step instructions
- **Conversation to Action** — Create autonomous actions directly from conversations with AI-guided setup
- **AWS Bedrock API Key Auth** — Support for explicit Access Key / Secret Key / Session Token authentication alongside SSO
- **Windows Code Signing** — Release workflow signs Windows exe and NSIS installer with SSL.com eSigner
- **4-Level Tool Approval** — Deny, Approve Once, Always (Conversation), Always (Global) with global_tool_permissions table
- **MCP UI Enhancements** — Edit MCP server configurations, view tools from dashboard, manage servers link
- **Tool Activity Date Grouping** — Sidecar entries grouped by date with collapsible headers and call counts
- **Resizable Sidecar** — Tools and Agents tabs with drag-to-resize handle; width persists in session
- **Run Now** — Execute autonomous actions immediately from the Actions page
- **Theme Persistence** — Dark/light theme preference saved to config.yaml, persists across restarts
- **Directory Browser** — Filesystem allowed_paths uses folder browser modal instead of text input
- **Skill Documentation** — Comprehensive tool docs for run_command, spawn_agent, list_provider_models, create_word, create_excel, create_powerpoint, create_pdf

### Fixed
- MCP stdio tool execution failing during conversations (event loop mismatch)
- Memory storage and dashboard using wrong user GUID
- Tool activity sidecar empty when reopening past conversations
- Filesystem allowed_paths stored as string instead of list
- macOS app PATH missing common tool directories (Homebrew, Docker)
- Daemon MCP connections — independent connections per action execution
- Scheduler timezone — cron schedules now use local timezone
- Scheduler sleep/wake recovery with stale lock clearing
- Email TLS string normalisation and secret resolution
- Agent provider isolation — concurrent conversations no longer conflict
- Settings tool config refresh without restart
- SSL auto-generates self-signed certificate when enabled without cert files

### Changed
- Autonomous action system prompt includes tool context, filesystem paths, OS info
- Autonomous action max_tokens guidance dynamically reflects configured limit
- Autonomous action tool iterations increased to 25 (was 10)
- Action run history records full activity log (tool calls + results)
- Max_tokens truncation triggers automatic retry with concise output instruction
- Prompt caching enabled for daemon action executor

## [0.1.0] - 2026-04-06

First production release of Spark -- Secure Personal AI Research Kit.

### Added

#### LLM Providers
- **Anthropic** (Direct API) -- Claude models with prompt caching support
- **AWS Bedrock** -- Claude and other foundation models via AWS
- **Google Gemini** -- dynamic model discovery from Gemini API
- **Ollama** -- local model support with automatic model detection
- **X.AI (Grok)** -- Grok models via OpenAI-compatible API
- Dynamic model discovery from provider APIs with static fallback
- Model list caching across all providers
- Transient error retry (503, 500, rate limits) with exponential backoff
- Prompt caching for Anthropic (cache_control blocks on system prompt and tools)
- Per-conversation prompt caching toggle with global default
- Cache stats displayed in UI (tokens read, created, savings percentage)

#### Conversations
- Real-time streaming via Server-Sent Events (SSE)
- Intelligent context compaction (LLM-driven summarisation)
- Conversation linking for shared context across conversations
- RAG retrieval from compacted history (filters active messages to avoid duplication)
- Favourites with star toggle
- Export as Markdown, HTML, CSV, or JSON
- Per-conversation settings: model, instructions, RAG, compaction thresholds
- Model badge on each assistant response showing which model generated it
- Model selector modal with search, grouping by provider, tool support indicators
- Intermediate tool-use messages preserved in separate bubbles

#### Tools
- **Tool Activity Sidecar Panel** -- dedicated right-side panel for tool calls with timestamps, expandable parameters and results
- Inline "Used N tools" indicator in chat linking to sidecar
- **Built-in tools:** filesystem (read/write), documents (Word, Excel, PDF, PowerPoint), archives (ZIP, TAR), web search/fetch, datetime
- **Web search engines:** DuckDuckGo (default, no API key), Brave Search, Google (SerpAPI), Bing (Azure), SearXNG (self-hosted)
- **Tool documentation system:** 22 markdown docs, get_tool_documentation tool for LLM self-reference
- **MCP integration:** stdio, HTTP streamable, and SSE transports with hot-reload
- Per-conversation tool enable/disable for embedded and MCP tools
- Category-based tool approval (approving web_search also approves web_fetch)
- Tool permission system with Allow Once, Always Allow, and Deny

#### Memory
- Persistent memory with semantic vector search (sentence-transformers)
- Categories: facts, preferences, projects, instructions, relationships
- Auto-retrieval of relevant memories injected into conversation context
- Proactive memory storage when user shares information
- Import/export as JSON
- Memory management page with stats, edit, delete, and delete-all

#### Voice
- **Speech-to-text input** -- mic button for manual dictation
- **Voice Conversation Mode** -- full hands-free operation via headset button
- Auto-send after 1.5s silence, TTS reads response, auto-resumes listening
- Voice selector dropdown with all available browser TTS voices (persisted)

#### Autonomous Actions
- Scheduled AI tasks with cron or one-off schedules
- AI-assisted action creation (describe what you want, Spark builds it)
- Context modes: fresh (clean each run) or cumulative (previous results in prompt)
- Failure tracking with auto-disable after threshold
- Run history with status, results, and token usage
- System tray daemon (macOS, Windows) with stats polling

#### Web Interface
- Cognisn design system (Bootstrap 5.3, dark/light theme with persistence)
- Dashboard with provider status cards and clickable provider models modal
- Settings page with providers, database, interface, conversation, security, tools, MCP
- Global system instructions
- Settings lock with password protection
- Secret reveal (eye toggle fetches actual value from server)
- Built-in searchable user guide
- Auto-update checker with rendered markdown release notes and download link
- Keyboard shortcuts

#### Security
- Prompt inspection with pattern matching (injection, jailbreak, PII detection)
- Configurable levels (basic, standard, strict) and actions (block, warn, log_only)
- Session-based authentication with cookie security (HTTPOnly, SameSite, Secure)
- API keys stored in OS keychain via cognisn-konfig

#### Platform
- **macOS:** signed and notarized DMG (ARM64, x86_64) with native splash screen
- **Windows:** NSIS installer with clean upgrade handling
- **Linux:** pip install
- PyApp distribution with Cognisn fork splash screen (v0.29.0-splash.4)

#### Database
- SQLite (default), MySQL, PostgreSQL, MSSQL
- Idempotent schema with automatic migrations

#### Documentation
- 16 comprehensive docs with Mermaid diagrams
- Covers installation, configuration, providers, conversations, tools, MCP, memory, actions, voice, web search, security, auto-update, architecture, development, API reference

#### Quality
- 398 automated tests across 19 test files
- SonarCloud: all A ratings (0 bugs, 0 vulnerabilities, 0 hotspots)
- CI on Ubuntu, macOS, Windows (Python 3.12, 3.13)

[0.2.0]: https://github.com/Cognisn/spark/releases/tag/v0.2.0
[0.1.0]: https://github.com/Cognisn/spark/releases/tag/v0.1.0
