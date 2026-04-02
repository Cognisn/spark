"""Settings / setup endpoints."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")

_LOCK_SECRET_KEY = "settings_lock_password"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _is_locked(request: Request) -> bool:
    """Check if settings are password-locked."""
    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not hasattr(ctx, "secrets") or not ctx.secrets:
        return False
    stored = ctx.secrets.get(_LOCK_SECRET_KEY)
    return bool(stored)


def _verify_lock(request: Request, password: str) -> bool:
    """Verify the settings lock password."""
    ctx = request.app.state.ctx
    stored = ctx.secrets.get(_LOCK_SECRET_KEY)
    if not stored:
        return True
    return _hash_password(password) == stored


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the settings page."""
    templates = request.app.state.templates
    ctx = request.app.state.ctx
    settings = ctx.settings

    is_locked = _is_locked(request)

    sections = _build_sections(settings)
    tool_categories = _build_tool_categories(settings)

    llm_manager = getattr(request.app.state, "llm_manager", None)
    available_models = llm_manager.list_all_models() if llm_manager else []

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sections": sections,
            "tool_categories": tool_categories,
            "available_models": available_models,
            "is_locked": is_locked,
        },
    )


@router.post("/api/lock")
async def set_lock(request: Request) -> JSONResponse:
    """Set or update the settings lock password."""
    data = await request.json()
    password = data.get("password", "").strip()
    current_password = data.get("current_password", "")

    ctx = request.app.state.ctx

    # If already locked, verify current password first
    if _is_locked(request):
        if not _verify_lock(request, current_password):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=403)

    if not password:
        # Remove lock
        try:
            ctx.secrets.delete(_LOCK_SECRET_KEY)
        except Exception:
            pass
        return JSONResponse({"status": "ok", "locked": False})

    # Set new password
    ctx.secrets.set(_LOCK_SECRET_KEY, _hash_password(password))
    return JSONResponse({"status": "ok", "locked": True})


@router.post("/api/unlock")
async def unlock_settings(request: Request) -> JSONResponse:
    """Verify the lock password to gain access."""
    data = await request.json()
    password = data.get("password", "")

    if _verify_lock(request, password):
        # Store unlock in session (using a simple flag on app state per session)
        session_id = request.cookies.get("spark_session", "")
        unlocked = getattr(request.app.state, "_settings_unlocked", set())
        unlocked.add(session_id)
        request.app.state._settings_unlocked = unlocked
        return JSONResponse({"status": "ok"})

    return JSONResponse({"error": "Incorrect password"}, status_code=403)


@router.get("/api/lock-status")
async def lock_status(request: Request) -> JSONResponse:
    """Check if settings are locked and whether current session has unlocked."""
    locked = _is_locked(request)
    session_id = request.cookies.get("spark_session", "")
    unlocked_sessions = getattr(request.app.state, "_settings_unlocked", set())
    unlocked = session_id in unlocked_sessions

    return JSONResponse({"locked": locked, "unlocked": unlocked})


def _handle_daemon_settings(data: dict, ctx: Any) -> None:
    """Start or stop the tray daemon based on settings changes."""
    import os

    daemon_keys = {"autonomous_actions.enabled", "daemon.enabled"}
    if not daemon_keys.intersection(data.keys()):
        return

    actions_enabled = ctx.settings.get("autonomous_actions.enabled", False)
    daemon_enabled = ctx.settings.get("daemon.enabled", False)

    if actions_enabled and daemon_enabled:
        # Start daemon if not running
        try:
            from spark.web.server import _start_tray_daemon_if_needed

            _start_tray_daemon_if_needed()
        except Exception as e:
            logger.warning("Failed to start daemon: %s", e)
    else:
        # Stop daemon if running
        try:
            from spark.core.application import _get_data_path

            pid_file = _get_data_path() / "spark_daemon_tray.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                import signal

                os.kill(pid, signal.SIGTERM)
                pid_file.unlink(missing_ok=True)
                logger.info("Tray daemon stopped (PID %d)", pid)
        except Exception as e:
            logger.debug("Daemon stop: %s", e)


# Keys that hold secret values — stored in secrets backend, not config.yaml
_SECRET_KEYS = {
    "providers.anthropic.api_key",
    "providers.google_gemini.api_key",
    "providers.xai.api_key",
    "database.password",
}


def _secret_name(dotted_key: str) -> str:
    """Derive a secret store key name from a dotted settings key.

    e.g. 'providers.google_gemini.api_key' → 'google_gemini_api_key'
    """
    parts = dotted_key.split(".")
    # Drop the top-level group (e.g. 'providers' or 'database')
    return "_".join(parts[1:])


@router.post("/api/save")
async def save_settings(request: Request) -> JSONResponse:
    """Persist settings changes to config.yaml."""
    data = await request.json()

    from spark.core.application import _get_config_path

    config_path = _get_config_path()

    try:
        # Load existing config
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text()) or {}
        else:
            raw = {}

        ctx = request.app.state.ctx

        # Apply changes — route secrets to the secrets backend
        for dotted_key, value in data.items():
            if dotted_key in _SECRET_KEYS and value:
                # Store the actual value in the secrets backend
                name = _secret_name(dotted_key)
                ctx.secrets.set(name, str(value))
                # Write a secret:// URI reference in the config
                _set_nested(raw, dotted_key, f"secret://{name}")
                logger.info("Stored secret for %s", dotted_key)
            else:
                _set_nested(raw, dotted_key, value)

        # Write back
        config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))

        # Reload settings in memory
        ctx.settings.reload()

        # Clear first-run flag
        request.app.state.first_run = False

        # Handle daemon start/stop based on settings changes
        _handle_daemon_settings(data, ctx)

        logger.info("Settings saved to %s", config_path)
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        logger.error("Failed to save settings: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=500)


def _set_nested(d: dict, dotted_key: str, value: object) -> None:
    """Set a value in a nested dict using a dot-separated key."""
    keys = dotted_key.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _build_sections(settings: object) -> list[dict]:
    """Build settings sections for the template."""
    return [
        {
            "id": "providers",
            "title": "LLM Providers",
            "icon": "bi-robot",
            "description": "Configure your AI model providers.",
            "groups": [
                {
                    "id": "anthropic",
                    "title": "Anthropic",
                    "fields": [
                        _toggle("providers.anthropic.enabled", "Enabled", settings),
                        _secret("providers.anthropic.api_key", "API Key", settings),
                    ],
                },
                {
                    "id": "aws_bedrock",
                    "title": "AWS Bedrock",
                    "fields": [
                        _toggle("providers.aws_bedrock.enabled", "Enabled", settings),
                        _text("providers.aws_bedrock.region", "Region", settings, "us-east-1"),
                        _select(
                            "providers.aws_bedrock.auth_method",
                            "Auth Method",
                            settings,
                            ["sso", "iam", "session"],
                            "sso",
                        ),
                    ],
                },
                {
                    "id": "ollama",
                    "title": "Ollama",
                    "fields": [
                        _toggle("providers.ollama.enabled", "Enabled", settings),
                        _text(
                            "providers.ollama.base_url",
                            "Base URL",
                            settings,
                            "http://localhost:11434",
                        ),
                    ],
                },
                {
                    "id": "google_gemini",
                    "title": "Google Gemini",
                    "fields": [
                        _toggle("providers.google_gemini.enabled", "Enabled", settings),
                        _secret("providers.google_gemini.api_key", "API Key", settings),
                    ],
                },
                {
                    "id": "xai",
                    "title": "X.AI",
                    "fields": [
                        _toggle("providers.xai.enabled", "Enabled", settings),
                        _secret("providers.xai.api_key", "API Key", settings),
                    ],
                },
            ],
        },
        {
            "id": "database",
            "title": "Database",
            "icon": "bi-database",
            "description": "Configure the database for conversations and memory.",
            "groups": [
                {
                    "id": "database",
                    "title": "Connection",
                    "fields": [
                        _select(
                            "database.type",
                            "Type",
                            settings,
                            ["sqlite", "mysql", "postgresql", "mssql"],
                            "sqlite",
                        ),
                        {**_text("database.host", "Host", settings, ""), "db_requires": "remote"},
                        {**_number("database.port", "Port", settings, ""), "db_requires": "remote"},
                        {
                            **_text("database.name", "Database Name", settings, ""),
                            "db_requires": "remote",
                        },
                        {
                            **_text("database.user", "Username", settings, ""),
                            "db_requires": "remote",
                        },
                        {
                            **_secret("database.password", "Password", settings),
                            "db_requires": "remote",
                        },
                    ],
                },
            ],
        },
        {
            "id": "interface",
            "title": "Web Interface",
            "icon": "bi-globe",
            "description": "Web server and session settings.",
            "groups": [
                {
                    "id": "server",
                    "title": "Server",
                    "fields": [
                        _toggle("interface.ssl.enabled", "Enable SSL", settings),
                        _number(
                            "interface.session_timeout_minutes",
                            "Session Timeout (minutes)",
                            settings,
                            60,
                        ),
                    ],
                },
            ],
        },
        {
            "id": "conversation",
            "title": "Conversation",
            "icon": "bi-chat-dots",
            "description": "Global instructions, context management, and tool execution settings.",
            "groups": [
                {
                    "id": "global_instructions",
                    "title": "Global Instructions",
                    "fields": [
                        _textarea(
                            "conversation.global_instructions",
                            "System Instructions",
                            settings,
                            "",
                            rows=6,
                            help_text="These instructions are prepended to every conversation alongside the built-in system prompt and any per-conversation instructions.",
                        ),
                    ],
                },
                {
                    "id": "context",
                    "title": "Context Compaction",
                    "fields": [
                        _number(
                            "conversation.rollup_threshold",
                            "Rollup Threshold",
                            settings,
                            0.3,
                        ),
                        _number(
                            "conversation.rollup_summary_ratio",
                            "Summary Ratio",
                            settings,
                            0.3,
                        ),
                        _number(
                            "conversation.emergency_rollup_threshold",
                            "Emergency Threshold",
                            settings,
                            0.95,
                        ),
                    ],
                },
                {
                    "id": "tools",
                    "title": "Tool Execution",
                    "fields": [
                        _number(
                            "conversation.max_tool_iterations",
                            "Max Tool Iterations",
                            settings,
                            25,
                        ),
                        _number(
                            "conversation.max_tool_selections",
                            "Max Tool Selections",
                            settings,
                            30,
                        ),
                        _number(
                            "conversation.max_tool_result_tokens",
                            "Max Result Tokens",
                            settings,
                            4000,
                        ),
                    ],
                },
            ],
        },
        {
            "id": "security",
            "title": "Security",
            "icon": "bi-shield-check",
            "description": "Prompt inspection and tool permissions.",
            "groups": [
                {
                    "id": "prompt_inspection",
                    "title": "Prompt Inspection",
                    "fields": [
                        _toggle("prompt_inspection.enabled", "Enabled", settings),
                        _select(
                            "prompt_inspection.level",
                            "Level",
                            settings,
                            ["basic", "standard", "strict"],
                            "standard",
                        ),
                        _select(
                            "prompt_inspection.action",
                            "Action",
                            settings,
                            ["block", "warn", "sanitize", "log_only"],
                            "warn",
                        ),
                    ],
                },
                {
                    "id": "tool_perms",
                    "title": "Tool Permissions",
                    "fields": [
                        _toggle("tool_permissions.auto_approve", "Auto-approve Tools", settings),
                    ],
                },
            ],
        },
        {
            "id": "logging",
            "title": "Logging",
            "icon": "bi-journal-text",
            "description": "Application logging configuration.",
            "groups": [
                {
                    "id": "logging",
                    "title": "Log Settings",
                    "fields": [
                        _select(
                            "logging.level",
                            "Level",
                            settings,
                            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                            "INFO",
                        ),
                        _select(
                            "logging.format",
                            "Format",
                            settings,
                            ["text", "json"],
                            "text",
                        ),
                        _number("logging.retention_runs", "Retention (runs)", settings, 10),
                    ],
                },
            ],
        },
        {
            "id": "default_model",
            "title": "Default Model",
            "icon": "bi-cpu",
            "description": "Set a default or mandatory model for new conversations.",
            "groups": [
                {
                    "id": "default_model",
                    "title": "Model Selection",
                    "fields": [
                        {
                            "type": "model_select",
                            "key": "default_model.model_id",
                            "label": "Default Model",
                            "value": _get_val(settings, "default_model.model_id", ""),
                        },
                        _select(
                            "default_model.mode",
                            "Mode",
                            settings,
                            ["default", "mandatory"],
                            "default",
                        ),
                    ],
                },
            ],
        },
        {
            "id": "autonomous_actions",
            "title": "Autonomous Actions",
            "icon": "bi-play-circle",
            "description": "Configure the autonomous action execution system.",
            "groups": [
                {
                    "id": "actions",
                    "title": "Actions",
                    "fields": [
                        _toggle(
                            "autonomous_actions.enabled", "Enable Autonomous Actions", settings
                        ),
                        _number(
                            "autonomous_actions.max_concurrent",
                            "Max Concurrent Executions",
                            settings,
                            3,
                        ),
                    ],
                },
                {
                    "id": "daemon",
                    "title": "Daemon",
                    "fields": [
                        _toggle("daemon.enabled", "Enable Background Daemon", settings),
                        _number(
                            "daemon.heartbeat_interval",
                            "Heartbeat Interval (seconds)",
                            settings,
                            30,
                        ),
                    ],
                },
            ],
        },
    ]


# -- Field helpers --


def _get_val(settings: object, key: str, default: object = None) -> object:
    return settings.get(key, default)  # type: ignore[union-attr]


def _toggle(key: str, label: str, settings: object) -> dict:
    return {"type": "toggle", "key": key, "label": label, "value": bool(_get_val(settings, key))}


def _text(key: str, label: str, settings: object, default: object = "") -> dict:
    return {"type": "text", "key": key, "label": label, "value": _get_val(settings, key, default)}


def _number(key: str, label: str, settings: object, default: object = "") -> dict:
    return {
        "type": "number",
        "key": key,
        "label": label,
        "value": _get_val(settings, key, default),
    }


def _textarea(
    key: str, label: str, settings: object, default: str = "", rows: int = 4, help_text: str = ""
) -> dict:
    return {
        "type": "textarea",
        "key": key,
        "label": label,
        "value": _get_val(settings, key, default) or "",
        "rows": rows,
        "help": help_text,
    }


def _secret(key: str, label: str, settings: object) -> dict:
    val = _get_val(settings, key, "")
    has_value = bool(val) and str(val) != ""
    return {"type": "secret", "key": key, "label": label, "has_value": has_value}


def _select(key: str, label: str, settings: object, options: list[str], default: str = "") -> dict:
    return {
        "type": "select",
        "key": key,
        "label": label,
        "value": _get_val(settings, key, default),
        "options": options,
    }


def _build_tool_categories(settings: object) -> list[dict]:
    """Build embedded tool category data for the tools management panel."""
    get = settings.get  # type: ignore[union-attr]

    return [
        {
            "id": "filesystem",
            "title": "Filesystem",
            "icon": "bi-folder2-open",
            "description": "Read, write, search, and inspect files and directories.",
            "enabled": bool(get("embedded_tools.filesystem.enabled", True)),
            "mode": get("embedded_tools.filesystem.mode", "read"),
            "mode_options": ["read", "read_write"],
            "mode_descriptions": {
                "read": "Read-only access to files within allowed paths.",
                "read_write": "Full read and write access. Use with caution.",
            },
            "extra_fields": [
                {
                    "key": "embedded_tools.filesystem.allowed_paths",
                    "label": "Allowed Paths",
                    "type": "text",
                    "value": ", ".join(get("embedded_tools.filesystem.allowed_paths", []) or []),
                    "help": "Comma-separated list of directories. Empty = working directory only.",
                },
            ],
            "tools": [
                "read_file",
                "list_directory",
                "search_files",
                "get_file_info",
                "find_in_file",
                "get_directory_tree",
                "write_file",
            ],
        },
        {
            "id": "documents",
            "title": "Documents",
            "icon": "bi-file-earmark-richtext",
            "description": "Read Word, Excel, PowerPoint, and PDF documents.",
            "enabled": bool(get("embedded_tools.documents.enabled", True)),
            "mode": get("embedded_tools.documents.mode", "read"),
            "mode_options": ["read", "read_write"],
            "mode_descriptions": {
                "read": "Read document contents only.",
                "read_write": "Read and create documents.",
            },
            "extra_fields": [
                {
                    "key": "embedded_tools.documents.max_file_size_mb",
                    "label": "Max File Size (MB)",
                    "type": "number",
                    "value": get("embedded_tools.documents.max_file_size_mb", 50),
                    "help": "Maximum file size in megabytes for document reading.",
                },
            ],
            "tools": ["read_word", "read_excel", "read_pdf", "read_powerpoint"],
        },
        {
            "id": "archives",
            "title": "Archives",
            "icon": "bi-file-zip",
            "description": "List and extract ZIP and TAR archive contents.",
            "enabled": bool(get("embedded_tools.archives.enabled", True)),
            "mode": get("embedded_tools.archives.mode", "list"),
            "mode_options": ["list", "extract"],
            "mode_descriptions": {
                "list": "List archive contents only.",
                "extract": "List and extract archive files.",
            },
            "extra_fields": [],
            "tools": ["list_archive", "extract_archive"],
        },
        {
            "id": "web",
            "title": "Web",
            "icon": "bi-globe2",
            "description": "Search the web and fetch page content.",
            "enabled": bool(get("embedded_tools.web.enabled", True)),
            "mode": None,
            "mode_options": [],
            "mode_descriptions": {},
            "extra_fields": [
                {
                    "key": "embedded_tools.web.search_engine",
                    "label": "Search Engine",
                    "type": "select",
                    "value": get("embedded_tools.web.search_engine", "duckduckgo"),
                    "options": [
                        {"value": "duckduckgo", "label": "DuckDuckGo (no API key)"},
                        {"value": "brave", "label": "Brave Search"},
                        {"value": "google", "label": "Google (SerpAPI)"},
                        {"value": "bing", "label": "Bing (Azure)"},
                        {"value": "searxng", "label": "SearXNG (self-hosted)"},
                    ],
                    "help": "DuckDuckGo requires no API key. Other engines need credentials configured below.",
                },
                {
                    "key": "embedded_tools.web.brave_api_key",
                    "label": "Brave API Key",
                    "type": "secret",
                    "value": get("embedded_tools.web.brave_api_key", ""),
                    "help": "Free tier at search.brave.com/api",
                    "show_when": "brave",
                },
                {
                    "key": "embedded_tools.web.google_api_key",
                    "label": "SerpAPI Key",
                    "type": "secret",
                    "value": get("embedded_tools.web.google_api_key", ""),
                    "help": "Get a key at serpapi.com",
                    "show_when": "google",
                },
                {
                    "key": "embedded_tools.web.bing_api_key",
                    "label": "Bing API Key",
                    "type": "secret",
                    "value": get("embedded_tools.web.bing_api_key", ""),
                    "help": "Azure Cognitive Services Bing Search key",
                    "show_when": "bing",
                },
                {
                    "key": "embedded_tools.web.searxng_url",
                    "label": "SearXNG URL",
                    "type": "text",
                    "value": get("embedded_tools.web.searxng_url", ""),
                    "help": "Full URL of your SearXNG instance, e.g. https://search.example.com",
                    "show_when": "searxng",
                },
            ],
            "tools": ["web_search", "web_fetch"],
        },
    ]
