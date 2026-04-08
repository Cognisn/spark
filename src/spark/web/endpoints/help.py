"""User guide and help endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/help")

# Structured help content — searchable by topic
HELP_TOPICS = [
    {
        "id": "getting-started",
        "title": "Getting Started",
        "category": "Basics",
        "content": """## Getting Started with Spark

Spark is a secure personal AI research kit that lets you interact with multiple AI models through a modern web interface.

### First Launch
1. When Spark starts for the first time, it creates a default configuration and opens your browser
2. You'll see a welcome page directing you to **Settings** to configure an LLM provider
3. Enable at least one provider (Anthropic, AWS Bedrock, Ollama, Google Gemini, or X.AI) and provide its API key
4. Once configured, create your first conversation from the **Conversations** page

### Navigation
- **Home** — Dashboard with recent conversations, provider status, and tool overview
- **Conversations** — List, search, create, and manage conversations
- **Memories** — View and manage persistent memories the AI stores
- **Actions** — Configure autonomous scheduled AI tasks
- **Settings** — Application configuration (providers, database, tools, security)

### Keyboard Shortcuts
- **Ctrl/Cmd + K** — Go to Conversations
- **Ctrl/Cmd + ,** — Open Settings
- **Enter** — Send message (in chat)
- **Shift + Enter** — New line (in chat)
""",
    },
    {
        "id": "conversations",
        "title": "Conversations",
        "category": "Features",
        "content": """## Conversations

### Creating a Conversation
Click **New Conversation** and choose a name and model. If a default model is configured in Settings, it will be pre-selected.

### Chat Interface
- Type your message and press **Enter** to send
- The AI will respond, potentially using tools (shown as expandable tool call cards)
- Token usage is shown at the bottom of the chat

### Conversation Settings (gear icon)
- **Instructions** — Custom system prompt for this conversation
- **Context** — RAG search settings, message history limits, tool result inclusion
- **Compaction** — Automatic context summarisation when approaching token limits
- **Tools** — Enable/disable specific tools for this conversation
- **Export** — Download conversation as Markdown, HTML, CSV, or JSON
- **Linked Conversations** — Share context between conversations

### Managing Conversations
- **Star** — Mark as favourite (appears at top of list and on dashboard)
- **Rename** — Click the conversation name in the chat header
- **Change Model** — Click the model name to switch models
- **Delete** — Trash icon in chat header or conversation list
- **Search** — Search bar on the Conversations page searches by name and content
""",
    },
    {
        "id": "providers",
        "title": "LLM Providers",
        "category": "Configuration",
        "content": """## LLM Providers

Spark supports multiple AI model providers. Each provider has a detailed setup guide to walk you through account creation, API key generation, and configuration.

### Anthropic (Direct API)
- Models: Claude Opus 4, Sonnet 4, 3.7 Sonnet, 3.5 Sonnet/Haiku
- Requires an API key from anthropic.com
- Supports streaming and tool use
- [View Anthropic Setup Guide](/help/provider/anthropic)

### AWS Bedrock
- Access Claude and other models via AWS
- Requires AWS credentials (SSO, IAM, or session)
- Supports region selection
- [View AWS Bedrock Setup Guide](/help/provider/aws_bedrock)

### Ollama (Local)
- Run models locally on your machine
- No API key needed — just the Ollama server URL
- Models: Llama, Mistral, Qwen, Gemma, etc.
- [View Ollama Setup Guide](/help/provider/ollama)

### Google Gemini
- Models: Gemini 2.5 Pro/Flash, 2.0 Flash, 1.5 Pro/Flash
- Requires an API key from Google AI Studio
- [View Google Gemini Setup Guide](/help/provider/google_gemini)

### X.AI (Grok)
- Models: Grok 4.1, Grok 4, Grok 3
- Requires an API key from x.ai
- [View X.AI Setup Guide](/help/provider/xai)

### API Key Security
API keys entered in Settings are stored in your OS keychain (macOS Keychain, Windows Credential Locker) — never in plain text config files.
""",
    },
    {
        "id": "tools",
        "title": "Tools",
        "category": "Features",
        "content": """## Tools

Spark provides the AI with tools it can use during conversations:

### Built-in Tools
- **DateTime** — Get current date/time in any timezone
- **Filesystem** — Read/write files, search directories (requires allowed_paths in Settings)
- **Documents** — Read Word, Excel, PowerPoint, and PDF files
- **Archives** — List and extract ZIP/TAR files
- **Web** — Search the web and fetch page content
- **Memory** — Store, query, list, and delete persistent memories

### MCP Servers
Connect external tool servers via the Model Context Protocol:
1. Go to **Settings → Tools → MCP Servers**
2. Click **Add Server** and configure the transport (stdio, HTTP, or SSE)
3. Use **Test Connection** to verify before saving
4. MCP tools become immediately available in conversations

### Per-Conversation Tool Control
In any conversation's Settings (gear icon) → Tools tab, you can enable/disable individual tools or entire MCP servers.
""",
    },
    {
        "id": "memory",
        "title": "Memory System",
        "category": "Features",
        "content": """## Memory System

Spark has a persistent memory system that remembers information across conversations.

### How It Works
- The AI automatically stores important facts, preferences, and context using the `store_memory` tool
- When you start a new message, relevant memories are automatically retrieved and included in the context
- You can also ask the AI to remember or forget things

### Managing Memories
Visit the **Memories** page to:
- View all stored memories with category badges and importance scores
- Filter by category (preferences, facts, projects, instructions, relationships)
- Edit or delete individual memories
- Import/export memories as JSON
- Delete all memories (requires typing DELETE_ALL to confirm)

### Categories
- **Preferences** — User preferences and settings
- **Facts** — Factual information about the user or topics
- **Projects** — Project-related context and decisions
- **Instructions** — Standing instructions for the AI
- **Relationships** — People, organisations, and connections
""",
    },
    {
        "id": "actions",
        "title": "Autonomous Actions",
        "category": "Features",
        "content": """## Autonomous Actions

Schedule AI tasks to run automatically in the background.

### Creating Actions
1. Go to the **Actions** page
2. Click **New Action** (manual form) or **Create with AI** (guided assistant)
3. Configure: name, model, prompt, schedule (cron or one-off), tools

### AI-Assisted Creation
The **Create with AI** button opens a chat where you describe what you want and the AI builds the action configuration for you.

### Schedules
- **One-off** — Runs once at a specific date/time
- **Recurring** — Cron expression (e.g., `*/10 * * * *` = every 10 minutes, `0 8 * * 1-5` = weekdays at 8am)

### Daemon
Enable the background daemon in **Settings → Autonomous Actions** to run actions even when the Spark web UI is closed. The daemon shows a system tray icon with status and controls.

### Managing Actions
- Enable/disable toggle per action
- View run history with results and token usage
- Edit action configuration and tool permissions
- Delete actions
""",
    },
    {
        "id": "security",
        "title": "Security",
        "category": "Configuration",
        "content": """## Security

### Prompt Inspection
Enable in **Settings → Security** to scan user messages for:
- Prompt injection attempts
- Jailbreak patterns
- Code injection
- PII (Social Security numbers, credit cards, etc.)

Actions: block (reject message), warn (flag but allow), sanitize, or log_only.

### Settings Lock
Click the lock icon on the Settings page to password-protect settings. The password is stored securely in the OS keychain.

### Tool Permissions
On first use of any tool, Spark asks for permission (allow once, always allow, or deny). Permissions are stored per-conversation.

### Authentication
The web UI uses one-time authentication codes displayed in the terminal. Auto-login via URL is used on startup.
""",
    },
    {
        "id": "data-locations",
        "title": "Data & Log Locations",
        "category": "Configuration",
        "content": """## Data & Log Locations

### macOS
- **Config:** ~/Library/Application Support/spark/config.yaml
- **Database:** ~/Library/Application Support/spark/spark.db
- **Logs:** ~/Library/Logs/spark/

### Linux
- **Config:** ~/.config/spark/config.yaml
- **Database:** ~/.local/share/spark/spark.db
- **Logs:** ~/.local/state/spark/logs/

### Windows
- **Config:** %APPDATA%/spark/config.yaml
- **Database:** %APPDATA%/spark/spark.db
- **Logs:** %LOCALAPPDATA%/spark/logs/

Use **Help → Open Log Folder** or **Open Data Folder** from the navigation bar.
""",
    },
    {
        "id": "shortcuts",
        "title": "Keyboard Shortcuts",
        "category": "Basics",
        "content": """## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| **Ctrl/Cmd + K** | Go to Conversations |
| **Ctrl/Cmd + N** | New Conversation |
| **Ctrl/Cmd + ,** | Open Settings |
| **Enter** | Send message (in chat) |
| **Shift + Enter** | New line in message |
| **Escape** | Close modal/dialog |
""",
    },
]


@router.get("", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    """Render the user guide page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "help.html", {"topics": HELP_TOPICS})


@router.get("/api/search")
async def search_help(request: Request) -> JSONResponse:
    """Search help topics."""
    query = request.query_params.get("q", "").strip().lower()
    if not query:
        return JSONResponse(HELP_TOPICS)

    results = []
    for topic in HELP_TOPICS:
        if (
            query in topic["title"].lower()
            or query in topic["category"].lower()
            or query in topic["content"].lower()
        ):
            results.append(topic)

    return JSONResponse(results)
