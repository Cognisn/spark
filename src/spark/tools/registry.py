"""Tool registry — assembles and dispatches all built-in tools."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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


def _has_paths(config: dict) -> bool:
    """Check if allowed_paths is configured and non-empty."""
    paths = config.get("allowed_paths")
    if isinstance(paths, list):
        return len(paths) > 0
    if isinstance(paths, str):
        return len(paths.strip()) > 0
    return False


def get_builtin_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all enabled built-in tool definitions.

    Tools that require a filesystem path are excluded when no
    ``allowed_paths`` are configured.
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

    # Filesystem — requires allowed_paths
    fs_config = embedded.get("filesystem", {})
    if fs_config.get("enabled", True) and _has_paths(fs_config):
        from spark.tools.filesystem import get_tools

        tools.extend(get_tools(mode=fs_config.get("mode", "read")))

    # Documents — requires filesystem allowed_paths (documents live on disk)
    doc_config = embedded.get("documents", {})
    if doc_config.get("enabled", True) and _has_paths(fs_config):
        from spark.tools.documents import get_tools as doc_tools

        tools.extend(doc_tools(mode=doc_config.get("mode", "read")))

    # Archives — requires filesystem allowed_paths
    arc_config = embedded.get("archives", {})
    if arc_config.get("enabled", True) and _has_paths(fs_config):
        from spark.tools.archives import get_tools as arc_tools

        tools.extend(arc_tools(mode=arc_config.get("mode", "list")))

    # Web — no path required
    web_config = embedded.get("web", {})
    if web_config.get("enabled", True):
        from spark.tools.web import get_tools as web_tools

        tools.extend(web_tools())

    # Email — requires SMTP configuration
    email_config = embedded.get("email", {})
    if email_config.get("enabled", False) and email_config.get("host"):
        from spark.tools.email_tool import get_tools as email_tools

        tools.extend(email_tools())

    # Memory — always available
    from spark.tools.memory_tools import TOOLS as mem_tools

    tools.extend(mem_tools)

    return tools


def execute_builtin_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, bool]:
    """Execute a built-in tool. Returns (result_text, is_error)."""
    embedded = config.get("embedded_tools", {})

    try:
        # Tool documentation
        if tool_name == "get_tool_documentation":
            return _get_tool_documentation(tool_input.get("tool_name", "")), False

        # Datetime
        if tool_name == "get_current_datetime":
            from spark.tools.datetime_tool import execute

            return execute(tool_input), False

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
                return f"Tool '{tool_name}' requires allowed_paths to be configured.", True

            from spark.tools.filesystem import execute

            mode = fs_config.get("mode", "read")
            return execute(tool_name, tool_input, allowed_paths=allowed, mode=mode), False

        # Documents
        doc_tools = {"read_word", "read_excel", "read_pdf", "read_powerpoint"}
        if tool_name in doc_tools:
            fs_config = embedded.get("filesystem", {})
            if not _has_paths(fs_config):
                return f"Tool '{tool_name}' requires allowed_paths to be configured.", True

            from spark.tools.documents import execute

            return execute(tool_name, tool_input), False

        # Archives
        if tool_name in ("list_archive", "extract_archive"):
            fs_config = embedded.get("filesystem", {})
            if not _has_paths(fs_config):
                return f"Tool '{tool_name}' requires allowed_paths to be configured.", True

            from spark.tools.archives import execute

            arc_mode = embedded.get("archives", {}).get("mode", "list")
            return execute(tool_name, tool_input, mode=arc_mode), False

        # Web
        if tool_name in ("web_search", "web_fetch"):
            from spark.tools.web import execute

            return execute(tool_name, tool_input, config), False

        # Email
        email_tools = {"send_email", "draft_email"}
        if tool_name in email_tools:
            email_cfg = embedded.get("email", {})
            if not email_cfg.get("enabled", False):
                return "Email tool is disabled. Enable it in Settings → Email.", True
            if not email_cfg.get("host"):
                return "Email SMTP host is not configured. Go to Settings → Email.", True

            from spark.tools.email_tool import execute as email_execute

            return email_execute(tool_name, tool_input, config), False

        # Memory
        memory_tool_names = {"store_memory", "query_memory", "list_memories", "delete_memory"}
        if tool_name in memory_tool_names:
            from spark.tools.memory_tools import execute as mem_execute

            # Memory index needs to be passed via runtime context
            memory_index = config.get("_memory_index")
            return mem_execute(tool_name, tool_input, memory_index), False

        return f"Unknown tool: {tool_name}", True

    except Exception as e:
        logger.error("Tool %s execution error: %s", tool_name, e)
        return f"Error executing {tool_name}: {e}", True
