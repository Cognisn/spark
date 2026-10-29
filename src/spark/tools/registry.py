"""Tool registry — assembles and dispatches all built-in tools.

Every built-in tool belongs to one category. A category is the unit of
availability: :func:`is_category_available` is the single place that decides
whether a category may be offered or run, and every route that registers or
dispatches a tool by name asks it (through :func:`is_tool_available` or
:func:`unavailable_reason`). Each category also names a permission,
``spark.tools.<category>``, for role checks to attach to.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from spark.database.files import QuotaExceeded
from spark.files import FileAreaNotConfigured, PathRefused
from spark.files.scope import ToolScopeUnavailable
from spark.mode import Mode

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_TOOL_REFUSALS",
    "LOCAL_ONLY_CATEGORIES",
    "TOOL_CATEGORIES",
    "execute_builtin_tool",
    "get_builtin_tools",
    "is_category_available",
    "is_tool_available",
    "permission_name",
    "tool_category",
    "unavailable_reason",
]

# Category -> the built-in tool names in it.
TOOL_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "filesystem": (
        "read_file",
        "write_file",
        "list_directory",
        "search_files",
        "get_file_info",
        "find_in_file",
        "get_directory_tree",
    ),
    "documents": (
        "read_word",
        "read_excel",
        "read_pdf",
        "read_powerpoint",
        "create_word",
        "create_excel",
        "create_powerpoint",
        "create_pdf",
    ),
    "archives": ("list_archive", "extract_archive"),
    "web": ("web_search", "web_fetch"),
    "system_commands": ("run_command",),
    "memory": ("store_memory", "query_memory", "list_memories", "delete_memory"),
    "email": ("send_email", "draft_email"),
    "skills": ("use_skill", "read_skill_resource"),
    "skill_authoring": ("create_skill", "update_skill"),
    "knowledge": ("query_knowledge_graph",),
    "core": ("get_current_datetime", "get_tool_documentation"),
    "agents": ("spawn_agent", "list_provider_models"),
}

# Categories that act on the machine Spark runs on (its shell, its disks, its
# mail account). They assume a locally running, single-user instance, so a
# server never offers them.
LOCAL_ONLY_CATEGORIES: frozenset[str] = frozenset({"filesystem", "system_commands", "email"})

_CATEGORY_LABELS: Mapping[str, str] = {
    "filesystem": "filesystem tools",
    "system_commands": "system command tools",
    "email": "email tools",
}

_CATEGORY_OF: dict[str, str] = {
    name: category for category, names in TOOL_CATEGORIES.items() for name in names
}


def tool_category(tool_name: str) -> str | None:
    """The category of a built-in tool, or ``None`` for a tool Spark does not provide."""
    return _CATEGORY_OF.get(tool_name)


def permission_name(category: str) -> str:
    """The permission that governs a category, for example ``spark.tools.web``."""
    return f"spark.tools.{category}"


def _current_mode() -> Mode:
    from spark import mode as spark_mode

    try:
        return spark_mode.current_mode()
    except RuntimeError:
        # Every entry point configures the mode at start-up; only code that
        # never started Spark (tests, scripts) reaches here, and Spark's
        # default mode is personal.
        return Mode.PERSONAL


def is_category_available(category: str, *, mode: Mode | None = None) -> bool:
    """Whether tools in ``category`` may be offered and run.

    This is the one decision point for tool availability. ``mode`` defaults
    to the configured deployment mode.
    """
    selected = mode if mode is not None else _current_mode()
    if selected is Mode.SERVER and category in LOCAL_ONLY_CATEGORIES:
        return False
    return True


def is_tool_available(tool_name: str, *, mode: Mode | None = None) -> bool:
    """Whether a tool may be offered and run; tools outside Spark's categories are not judged here."""
    category = tool_category(tool_name)
    return category is None or is_category_available(category, mode=mode)


def unavailable_reason(tool_name: str, *, mode: Mode | None = None) -> str | None:
    """The refusal message for a tool that may not run, or ``None`` when it may."""
    category = tool_category(tool_name)
    if category is None or is_category_available(category, mode=mode):
        return None
    label = _CATEGORY_LABELS.get(category, f"{category} tools")
    return f"Tool '{tool_name}' is not available: {label} are not available in server mode."


# Refusals from the file tools, reported to the model as they are.
FILE_TOOL_REFUSALS = (PathRefused, QuotaExceeded, ToolScopeUnavailable, FileAreaNotConfigured)


def _get_tool_documentation(tool_name: str) -> str:
    """Read tool documentation from the resources/tool_docs directory."""
    from pathlib import Path

    docs_dir = Path(__file__).parent.parent / "resources" / "tool_docs"

    if not tool_name:
        return "Please provide a tool_name parameter."

    # Sanitize — only allow alphanumeric, underscore, and leading underscore
    safe_name = "".join(c for c in tool_name if c.isalnum() or c == "_")
    doc_file = docs_dir / f"{safe_name}.md"

    if doc_file.exists():
        return doc_file.read_text(encoding="utf-8")

    # List available docs
    available = sorted(f.stem for f in docs_dir.glob("*.md") if not f.stem.startswith("_template"))
    return (
        f"Documentation not found for tool: {tool_name}\n\n"
        f"Available documentation: {', '.join(available)}\n\n"
        f"Tip: Use '_index' to see the full tool index."
    )


def _has_paths(config: dict[str, Any]) -> bool:
    """Check if allowed_paths is configured and non-empty."""
    paths = config.get("allowed_paths")
    if isinstance(paths, list):
        return len(paths) > 0
    if isinstance(paths, str):
        return len(paths.strip()) > 0
    return False


def _server_mode() -> bool:
    """Whether this process runs in server mode.

    A process that never configured its mode (a standalone tool call or a test)
    follows personal-mode rules, which still confine file tools to the allowed
    roots.
    """
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


def _file_tools_available(fs_config: dict[str, Any]) -> bool:
    """Document and archive tools work in the user's area (server) or the allowed roots."""
    return _server_mode() or _has_paths(fs_config)


def _file_scope(config: dict[str, Any]) -> Any:
    from spark.files.scope import tool_scope

    return tool_scope(config, server=_server_mode())


def get_builtin_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all enabled built-in tool definitions.

    Tools that require a filesystem path are excluded when no
    ``allowed_paths`` are configured, and tools in a category that is not
    available (see :func:`is_category_available`) are never registered.
    """
    tools: list[dict[str, Any]] = []
    embedded = config.get("embedded_tools", {})

    # Always available
    from spark.tools.datetime_tool import TOOLS as dt_tools

    tools.extend(dt_tools)

    # Tool documentation — always available
    tools.append(
        {
            "name": "get_tool_documentation",
            "description": "Retrieve detailed documentation for any embedded tool. Use this to learn about a tool's parameters, return values, examples, and best practices before using it. Pass '_index' as tool_name to get the full tool index.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the tool to get documentation for, or '_index' for the full index.",
                    }
                },
                "required": ["tool_name"],
            },
        }
    )

    # Knowledge graph query — always available (resolves scopes at call time)
    try:
        from spark.knowledge.tools import get_tools as kg_get_tools

        tools.extend(kg_get_tools())
    except Exception as e:  # noqa: BLE001 - graphs must never break tool assembly
        logger.warning("Knowledge graph tool unavailable: %s", e)

    # Skills — always available (degrades to none if the directory is unreadable)
    try:
        from spark.skills.tools import get_tools as skills_get_tools

        tools.extend(skills_get_tools())
    except Exception as e:  # noqa: BLE001 - skills must never break tool assembly
        logger.warning("Skills tools unavailable: %s", e)

    # Filesystem — requires allowed_paths
    fs_config = embedded.get("filesystem", {})
    if fs_config.get("enabled", True) and _has_paths(fs_config):
        from spark.tools.filesystem import get_tools

        tools.extend(get_tools(mode=fs_config.get("mode", "read")))

    # Documents — the user's file area (server) or filesystem allowed_paths
    doc_config = embedded.get("documents", {})
    if doc_config.get("enabled", True) and _file_tools_available(fs_config):
        from spark.tools.documents import get_tools as doc_tools

        tools.extend(doc_tools(mode=doc_config.get("mode", "read")))

        # Document creation tools (when mode is read_write)
        if doc_config.get("mode", "read") == "read_write":
            from spark.tools.document_creation import get_tools as doc_create_tools

            tools.extend(doc_create_tools())

    # Archives — the user's file area (server) or filesystem allowed_paths
    arc_config = embedded.get("archives", {})
    if arc_config.get("enabled", True) and _file_tools_available(fs_config):
        from spark.tools.archives import get_tools as arc_tools

        tools.extend(arc_tools(mode=arc_config.get("mode", "list")))

    # Web — no path required
    web_config = embedded.get("web", {})
    if web_config.get("enabled", True):
        from spark.tools.web import get_tools as web_tools

        tools.extend(web_tools())

    # System Commands — requires explicit enable
    cmd_config = embedded.get("system_commands", {})
    if cmd_config.get("enabled", False):
        from spark.tools.system_command import get_tools as cmd_tools

        tools.extend(cmd_tools())

    # Email — requires SMTP configuration
    email_config = embedded.get("email", {})
    if email_config.get("enabled", False) and email_config.get("host"):
        from spark.tools.email_tool import get_tools as email_tools

        tools.extend(email_tools())

    # Memory — always available
    from spark.tools.memory_tools import TOOLS as mem_tools

    tools.extend(mem_tools)

    return [t for t in tools if is_tool_available(t["name"])]


def execute_builtin_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, bool]:
    """Execute a built-in tool. Returns (result_text, is_error)."""
    refusal = unavailable_reason(tool_name)
    if refusal is not None:
        return refusal, True

    embedded = config.get("embedded_tools", {})

    try:
        # Tool documentation
        if tool_name == "get_tool_documentation":
            return _get_tool_documentation(tool_input.get("tool_name", "")), False

        # Knowledge graph
        if tool_name == "query_knowledge_graph":
            from spark.knowledge.tools import execute as kg_execute

            return kg_execute(tool_name, tool_input, config)

        # Skills
        from spark.skills.tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES

        if tool_name in (READ_TOOL_NAMES | WRITE_TOOL_NAMES):
            from spark.skills.tools import execute as skills_execute

            return skills_execute(tool_name, tool_input, config)

        # Datetime
        if tool_name == "get_current_datetime":
            from spark.tools.datetime_tool import execute as dt_execute

            return dt_execute(tool_input), False

        # Filesystem
        fs_tools = {
            "read_file",
            "write_file",
            "list_directory",
            "search_files",
            "get_file_info",
            "find_in_file",
            "get_directory_tree",
        }
        if tool_name in fs_tools:
            fs_config = embedded.get("filesystem", {})
            allowed = fs_config.get("allowed_paths", [])
            # Handle legacy string format (comma-separated)
            if isinstance(allowed, str):
                allowed = [p.strip() for p in allowed.split(",") if p.strip()]
            if not allowed:
                return (
                    f"Tool '{tool_name}' requires allowed_paths to be configured.",
                    True,
                )

            from spark.tools.filesystem import execute as fs_execute

            fs_mode = fs_config.get("mode", "read")
            return fs_execute(tool_name, tool_input, allowed_paths=allowed, mode=fs_mode), False

        # Documents (read)
        doc_read_tools = {"read_word", "read_excel", "read_pdf", "read_powerpoint"}
        if tool_name in doc_read_tools:
            from spark.tools.documents import execute as doc_execute

            return doc_execute(tool_name, tool_input, scope=_file_scope(config)), False

        # Documents (create)
        doc_create_tools = {
            "create_word",
            "create_excel",
            "create_powerpoint",
            "create_pdf",
        }
        if tool_name in doc_create_tools:
            from spark.tools.document_creation import execute as doc_create_execute

            return doc_create_execute(tool_name, tool_input, scope=_file_scope(config)), False

        # Archives
        if tool_name in ("list_archive", "extract_archive"):
            from spark.tools.archives import execute as arc_execute

            arc_mode = embedded.get("archives", {}).get("mode", "list")
            return (
                arc_execute(tool_name, tool_input, mode=arc_mode, scope=_file_scope(config)),
                False,
            )

        # Web
        if tool_name in ("web_search", "web_fetch"):
            from spark.tools.web import execute as web_execute

            return web_execute(tool_name, tool_input, config), False

        # System Commands
        if tool_name == "run_command":
            cmd_config = embedded.get("system_commands", {})
            if not cmd_config.get("enabled", False):
                return "System command tool is disabled. Enable in Settings.", True
            from spark.tools.system_command import execute as cmd_execute

            return cmd_execute(tool_name, tool_input, cmd_config), False

        # Email
        email_tools = {"send_email", "draft_email"}
        if tool_name in email_tools:
            email_cfg = embedded.get("email", {})
            if not email_cfg.get("enabled", False):
                return "Email tool is disabled. Enable it in Settings → Email.", True
            if not email_cfg.get("host"):
                return (
                    "Email SMTP host is not configured. Go to Settings → Email.",
                    True,
                )

            from spark.tools.email_tool import execute as email_execute

            return email_execute(tool_name, tool_input, config), False

        # Memory
        memory_tool_names = {
            "store_memory",
            "query_memory",
            "list_memories",
            "delete_memory",
        }
        if tool_name in memory_tool_names:
            from spark.tools.memory_tools import execute as mem_execute

            # Memory index needs to be passed via runtime context
            memory_index = config.get("_memory_index")
            return mem_execute(tool_name, tool_input, memory_index), False

        return f"Unknown tool: {tool_name}", True

    except FILE_TOOL_REFUSALS as e:
        return str(e), True
    except Exception as e:
        logger.error("Tool %s execution error: %s", tool_name, e)
        return f"Error executing {tool_name}: {e}", True
