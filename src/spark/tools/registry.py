"""Tool registry — assembles and dispatches all built-in tools."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
        # Datetime
        if tool_name == "get_current_datetime":
            from spark.tools.datetime_tool import execute

            return execute(tool_input), False

        # Filesystem
        fs_tools = {
            "read_file", "write_file", "list_directory", "search_files",
            "get_file_info", "find_in_file", "get_directory_tree",
        }
        if tool_name in fs_tools:
            fs_config = embedded.get("filesystem", {})
            allowed = fs_config.get("allowed_paths", [])
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
