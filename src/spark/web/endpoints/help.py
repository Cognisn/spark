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
2. You'll see a welcome page directing you to **Settings**, where **Connections** is the first section
3. Add at least one connection: choose **Add connection**, pick a service (Amazon Bedrock, Claude through Amazon Bedrock (mantle), Anthropic, Claude in Microsoft Foundry, OpenAI, X.AI, Microsoft Foundry, Google Gemini or Ollama), give it a name and enter its credentials
4. Open the connection's **Models** to choose the models it offers, then create your first conversation from the **Conversations** page

### Navigation
- **Home** — Dashboard with recent conversations, your model connections, and tool overview
- **Conversations** — List, search, create, and manage conversations
- **Memories** — View and manage persistent memories the AI stores
- **Actions** — Configure autonomous scheduled AI tasks
- **Settings** — Application configuration (connections, tools, security and more)

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
- **Change Model** — Click the model name to switch models; models are grouped by connection
- **Delete** — Trash icon in chat header or conversation list
- **Search** — Search bar on the Conversations page searches by name and content
""",
    },
    {
        "id": "providers",
        "title": "Connections",
        "category": "Configuration",
        "content": """## Connections

Spark reaches models through **connections**. A connection is one named service with its own settings, credentials, Region and residency policy, and the models you choose to offer. You can add several, even several of one service (a work and a personal key, or one Bedrock connection per Region).

### Adding a connection
1. Personal mode: open **Settings → Connections**. A server: an administrator opens **Account → Connections**
2. Choose **Add connection** and pick one of nine services: Amazon Bedrock, Claude through Amazon Bedrock (mantle), Anthropic, Claude in Microsoft Foundry, OpenAI, X.AI, Microsoft Foundry (OpenAI-compatible), Google Gemini or Ollama
3. Give it a name and fill in the service's fields (an API key, or a Region and an authentication mode for Bedrock, or a base URL for Ollama)
4. Save. Spark checks the settings, then which models the connection can reach

### Models
- **Models** on a connection lists the models Spark's catalogue has for the service and Region, each marked reachable or not by the last check
- Every model the residency policy allows is offered until you turn some off; a model outside the policy is shown as refused, with the reason
- Each model has a tier (lower, standard or higher), shown in the model picker
- A reachable model the catalogue lacks (most Ollama models) is listed apart, with a form to declare it

### Residency
Each connection says where its models may process requests. By default Bedrock, mantle and Microsoft Foundry connections stay in their own Region, the direct APIs allow global processing, and Ollama is local only. Change it in the connection's form.

### Credits (server mode)
On a server, an administrator assigns each connection to roles or named users, and may meter it: each call then costs credits, and you have a monthly allowance on it. The model picker shows the credits you have left, a warning appears at 80%, and new requests on that connection stop at 100% until the allowance resets on the first of the month (UTC). Personal mode has no credits or limits.

### Setup guides
Each guide walks through creating an account and getting a key:
- [Anthropic](/help/provider/anthropic)
- [Amazon Bedrock](/help/provider/aws_bedrock)
- [Ollama](/help/provider/ollama)
- [Google Gemini](/help/provider/google_gemini)
- [X.AI](/help/provider/xai)

### Credential security
A key you type into a connection goes straight to your OS keychain (macOS Keychain, Windows Credential Locker), or a server's secrets store, and is never shown again. It is never written to a config file in plain text.
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
