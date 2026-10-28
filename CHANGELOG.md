# Changelog

All notable changes to Spark will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Deployment mode**: a `mode` setting (`personal` or `server`, environment `SPARK__MODE`) read once at start-up through konfig. Personal mode keeps today's defaults (SQLite in the platform data directory). Server mode requires `database.url` to be a PostgreSQL URL and refuses to start otherwise
- **Versioned migrations**: an Alembic chain for SPARK, kept in a `spark` PostgreSQL schema with its own version table (the default schema on SQLite). The baseline reproduces the 0.2.0 schema exactly. `spark migrate` runs the chain (and, from a later release, warden's)

### Changed
- **Upgrading from 0.2.x (personal mode)**: on first start SPARK upgrades the existing SQLite database once: it applies 0.2.0's own schema statements, checks every column and index against the 0.2.0 baseline, and stamps the database. If anything differs it refuses with a list of the differences and changes nothing. Later schema changes are applied automatically at start-up in personal mode; in server mode run `spark migrate` first (SPARK refuses to start on a database behind the current schema)
- **Async data layer** (`spark.db`): a psycopg 3 connection pool for PostgreSQL and a WAL-mode aiosqlite pool for SQLite, replacing the single shared connection whose cursors could be read by another thread (the knowledge-graph status race). Synchronous code reaches it through one documented bridge. New runtime dependencies: `aiosqlite`, `psycopg[binary,pool]` and `alembic`
- README installation instructions now cover PyPI and uv only; the standalone download table is replaced by a note that standalone builds are paused

## [0.2.1] - 2026-10-28

0.2.0 was released on GitHub but did not reach PyPI. 0.2.1 is the first PyPI release of the 0.2 line and contains everything listed under 0.2.0.

### Fixed
- Releases publish to PyPI again. The pinned PyPI publish action used a twine that rejects the wheel metadata (version 2.5) written by current hatchling; it is now pinned to v1.14.3. The publish step no longer ignores errors, so a failed upload fails the release run, and re-releasing an existing version is skipped instead

## [0.2.0] - 2026-10-28

First stable release since 0.1.0. It consolidates the betas 0.2.0b1 to 0.2.0b17, released between 15 April and 26 July 2026, and is distributed through PyPI only: install it with `pip install cognisn-spark` or `uv tool install cognisn-spark`.

### Added
- **Conversation types**: conversation creation offers a type selection (Standard chat, Debate or Panel) as the framework for further chat styles
- **Debate mode**: a Pro and an Against agent, each with its own model, brief and full tool access, argue a user-defined topic across rounds and present structured evidence exhibits, while a Judge agent moderates, rules on the presented arguments only and answers follow-up questions; three-pane UI with per-turn cancellation and pause and resume
- **Debate agent capabilities**: per-agent tool and skill allowlists chosen at debate creation (judge: skills only), enabling asymmetric debates
- **Debate view polish**: per-pane chat-style tool activity groups, auto-scroll toggles, a highlighted Final Judgement card, Markdown and HTML export, and a floor indicator glow on the active agent's pane
- **Panel conversations**: 2 to 5 named AI panellists (per-panellist models, briefs and capability allowlists) plus an optional human panellist discuss a topic in moderator-directed rounds with shared visibility, a moderated synthesis, follow-up questions, a threaded discussion UI with a speaker rail, and Markdown and HTML export
- **Skills**: Claude-compatible SKILL.md skill folders with bundled resources; enabled skills are advertised in every LLM surface (chats, sub-agents, autonomous actions, debates) and loaded on demand via `use_skill`; `/skill-name` explicit trigger; approval-gated `create_skill` and `update_skill` authoring behind a bundled skill-creator skill; Skills management page with import, export and per-conversation toggles
- **Knowledge graphs**: two-level knowledge graphs built on demand by LLM extraction: a global graph over all conversations and memories, optional per-conversation graphs (a creation-time option), per-conversation opt-out of the global graph, and link-based pooling of conversation graphs; queryable by the model via `query_knowledge_graph` with automatic injection of relevant subgraph context; Knowledge page with build controls and an interactive force-directed visualisation
- **ElevenLabs voice (optional)**: natural text-to-speech as an opt-in integration. Enable it in Settings with an API key (stored in the OS keychain, never sent to the browser) and voice mode speaks with natural voices instead of the browser synthesiser, which remains the default and the automatic fallback on any error, quota exhaustion or missing key. Voice mode now works in debate and panel as well as chat, with a distinct voice per agent chosen at creation, three interaction modes (listen-along, immersive, listen-only), a monthly character cap, and an audio cache so repeated text is never billed twice
- **Run with uv**: Spark can be installed and run with `uv tool install cognisn-spark` (or `uvx cognisn-spark`); a `cognisn-spark` command alias makes the natural `uvx` invocation work. New `interface.port` and `interface.open_browser` settings (overridable via `SPARK__INTERFACE__PORT` and `SPARK__INTERFACE__OPEN_BROWSER`) allow a fixed port and a suppressed browser for headless and server runs. A CI smoke test installs the built wheel with uv and confirms it launches
- **System command tool**: execute shell commands (git, docker, aws, curl and so on) with OS-aware execution, a blocked command list and configurable approval prompts
- **Agent spawning**: the LLM can spawn independent sub-agents via the `spawn_agent` tool, with a dedicated Agents tab in the resizable sidecar panel; supports orchestrator-workers and chain modes; model auto-selection with user approval
- **Email tool**: send and draft emails via SMTP with HTML or plain text, to, cc and bcc, and file attachments; SMTP test connection button; passwords stored in the OS keychain
- **Document creation**: create Word (.docx), Excel (.xlsx), PowerPoint (.pptx) and PDF documents with advanced formatting (headings, tables, lists, images, styles)
- **Provider setup guides**: in-app setup guides for all LLM providers (Anthropic, AWS Bedrock, Ollama, Google Gemini, X.AI) with step-by-step instructions
- **Conversation to action**: create autonomous actions directly from conversations with AI-guided setup
- **AWS Bedrock API key authentication**: explicit access key, secret key and session token authentication alongside SSO
- **Windows code signing**: the release workflow signs the Windows executable and NSIS installer with SSL.com eSigner (binary builds are currently disabled; see Changed)
- **4-level tool approval**: Deny, Approve Once, Always (Conversation) and Always (Global), with a `global_tool_permissions` table
- **MCP UI enhancements**: edit MCP server configurations, view tools from the dashboard, and a manage servers link
- **Tool activity date grouping**: sidecar entries grouped by date with collapsible headers and call counts
- **Resizable sidecar**: Tools and Agents tabs with a drag-to-resize handle; the width persists in the session
- **Run now**: execute autonomous actions immediately from the Actions page
- **Theme persistence**: the dark or light theme preference is saved to config.yaml and persists across restarts
- **Directory browser**: filesystem `allowed_paths` uses a folder browser modal instead of a text input
- **Tool documentation**: documentation for `run_command`, `spawn_agent`, `list_provider_models`, `create_word`, `create_excel`, `create_powerpoint` and `create_pdf`
- Development tooling: `scripts/stamp_version.py` stamps a version and moves `[Unreleased]` into a dated changelog section, refusing a version that does not follow the current one under PEP 440; a worktree recipe at `dev/worktree.json`; contributor rules and project configuration in `CLAUDE.md`; `.gitattributes` enforcing LF line endings

### Changed
- **Releases are PyPI only**: the standalone macOS and Windows binaries, DMG and NSIS installers are not built for this release
- **Skill loading is transparent**: loading a skill (`use_skill` or `read_skill_resource`) no longer pauses for approval; it runs silently as a read-only progressive-disclosure step, in chats, debates, panels and sub-agents. Authoring skills (`create_skill` or `update_skill`) is still approval-gated
- **Debate and panel capabilities honour global skill settings**: the per-agent capability checklists in the debate and panel creation wizards list only skills that are enabled in the global settings, so a globally disabled skill no longer appears there (tools were already gated by their category settings)
- **Per-conversation skills control is easier to find**: in a conversation's tools panel, the Skills section is a distinct, labelled card with an enabled count and a note that it defaults to your global settings
- Autonomous action system prompt includes tool context, filesystem paths and OS information
- Autonomous action max_tokens guidance reflects the configured limit
- Autonomous action tool iterations increased to 25 (was 10)
- Action run history records the full activity log (tool calls and results)
- Max_tokens truncation triggers an automatic retry with a concise output instruction
- Prompt caching enabled for the daemon action executor
- CI now runs on pushes to release and workstream branches and on pull requests to release branches, as well as on `main`
- The `web_fetch` and `web_search` tool documentation now uses LF line endings, like the rest of the tree

### Fixed
- Debate and panel turns could fail with "No submit_argument call" (or discard a panellist's contribution) when the agent spent its whole tool budget researching, often after repeated web searches returned no results, and never reached the final submission tool. Agents now get a firm, tool-restricted prompt to submit before a turn is abandoned, and debaters and panellists have a larger research budget (25 tool iterations), so heavy research no longer throws the turn away
- Debate and panel modes now use prompt caching (on providers that support it: Anthropic, AWS Bedrock and Google Gemini) for every debater, panellist, judge and moderator turn. The system prompt and tool definitions are cached and reused across a turn's tool calls and across rounds, cutting token cost and latency. Previously these turns never requested caching, even with it enabled globally; they now follow the same `conversation.prompt_caching` setting as normal chats
- AWS Bedrock now honours prompt caching. The Converse provider previously ignored the caching flag entirely; it now inserts `cachePoint` blocks on the system prompt and tool definitions, reports cache read and write token metrics, and safely retries without caching for models that do not support it
- Debate and panel turns failed with "temperature is deprecated for this model" (HTTP 400) on the Claude 5 family (Sonnet 5, Fable 5) and Opus 4.7 and 4.8. The Anthropic Direct provider no longer sends the `temperature` parameter to models that reject it, and defensively retries without it if a model returns 400 on temperature
- Email settings `use_tls` and `require_approval` were never coerced to booleans, because a duplicate `_BOOL_STRING_KEYS` declaration silently overwrote the first
- Home page conversation lists now open debate and panel conversations in their own views (they were opening the standard chat view)
- Debate view now renders markdown in all three panes (was plain text)
- MCP stdio tool execution failing during conversations (event loop mismatch)
- Memory storage and dashboard using the wrong user GUID
- Tool activity sidecar empty when reopening past conversations
- Filesystem `allowed_paths` stored as a string instead of a list
- macOS app PATH missing common tool directories (Homebrew, Docker)
- Daemon MCP connections: independent connections per action execution
- Scheduler timezone: cron schedules now use the local timezone
- Scheduler sleep and wake recovery with stale lock clearing
- Email TLS string normalisation and secret resolution
- Agent provider isolation: concurrent conversations no longer conflict
- Settings tool configuration refreshes without a restart
- SSL auto-generates a self-signed certificate when enabled without certificate files

### Beta history
- 0.2.0b1 to 0.2.0b11 (15 April to 10 July 2026): system command tool, agent spawning, email and document creation tools, provider setup guides, AWS Bedrock key authentication, 4-level tool approval, sidecar and MCP UI improvements, and scheduler, daemon, MCP and settings fixes
- 0.2.0b12 (10 July 2026): conversation types, debate mode, panel conversations, skills and knowledge graphs
- 0.2.0b13 (14 July 2026): optional ElevenLabs voice; email settings and home page fixes
- 0.2.0b14 (23 July 2026): run with uv; releases become PyPI only
- 0.2.0b15 (25 July 2026): temperature fix for models that reject the parameter
- 0.2.0b16 (25 July 2026): transparent skill loading and skills configuration changes
- 0.2.0b17 (26 July 2026): forced final submission in debate and panel turns; prompt caching for debate, panel and AWS Bedrock

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

[0.2.1]: https://github.com/Cognisn/spark/releases/tag/v0.2.1
[0.2.0]: https://github.com/Cognisn/spark/releases/tag/v0.2.0
[0.1.0]: https://github.com/Cognisn/spark/releases/tag/v0.1.0
