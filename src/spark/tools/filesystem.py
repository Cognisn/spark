"""Filesystem tools — read, write, search, and inspect files."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

_READ_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a text file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read."},
                "max_lines": {"type": "integer", "description": "Max lines to read. Default: all."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories at a path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path. Default: current directory."},
                "recursive": {"type": "boolean", "description": "List recursively. Default: false."},
            },
        },
    },
    {
        "name": "search_files",
        "description": "Search for files matching a pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py', 'test_*.txt')."},
                "path": {"type": "string", "description": "Directory to search. Default: current directory."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "get_file_info",
        "description": "Get metadata about a file (size, type, modification time).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "find_in_file",
        "description": "Search for text within a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to search."},
                "query": {"type": "string", "description": "Text or regex to find."},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive search. Default: false."},
            },
            "required": ["path", "query"],
        },
    },
    {
        "name": "get_directory_tree",
        "description": "Display a visual directory tree structure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root directory. Default: current directory."},
                "max_depth": {"type": "integer", "description": "Max depth. Default: 3."},
            },
        },
    },
]

_WRITE_TOOLS = [
    {
        "name": "write_file",
        "description": "Write text content to a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write."},
                "content": {"type": "string", "description": "Text content to write."},
            },
            "required": ["path", "content"],
        },
    },
]


def get_tools(mode: str = "read") -> list[dict[str, Any]]:
    """Return filesystem tool definitions based on access mode."""
    tools = list(_READ_TOOLS)
    if mode == "read_write":
        tools.extend(_WRITE_TOOLS)
    return tools


def execute(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    allowed_paths: list[str] | None = None,
    mode: str = "read",
) -> str:
    """Execute a filesystem tool."""
    # Resolve and validate path
    path_str = tool_input.get("path", ".")
    path = Path(path_str).resolve()

    if allowed_paths:
        if not any(str(path).startswith(str(Path(ap).resolve())) for ap in allowed_paths):
            return f"Access denied: {path} is outside allowed paths."
    else:
        cwd = Path.cwd().resolve()
        if not str(path).startswith(str(cwd)):
            return f"Access denied: {path} is outside the working directory."

    if tool_name == "read_file":
        return _read_file(path, tool_input.get("max_lines"))
    elif tool_name == "list_directory":
        return _list_directory(path, tool_input.get("recursive", False))
    elif tool_name == "search_files":
        return _search_files(path, tool_input.get("pattern", "*"))
    elif tool_name == "get_file_info":
        return _get_file_info(path)
    elif tool_name == "find_in_file":
        return _find_in_file(path, tool_input["query"], tool_input.get("case_sensitive", False))
    elif tool_name == "get_directory_tree":
        return _get_directory_tree(path, tool_input.get("max_depth", 3))
    elif tool_name == "write_file":
        if mode != "read_write":
            return "Write access is not enabled."
        return _write_file(path, tool_input["content"])

    return f"Unknown filesystem tool: {tool_name}"


def _read_file(path: Path, max_lines: int | None) -> str:
    if not path.is_file():
        return f"File not found: {path}"
    try:
        text = path.read_text(encoding="utf-8")
        if max_lines:
            lines = text.splitlines(keepends=True)[:max_lines]
            text = "".join(lines)
            if len(lines) == max_lines:
                text += f"\n... (showing first {max_lines} lines)"
        return text
    except UnicodeDecodeError:
        return f"Cannot read {path}: not a text file."


def _list_directory(path: Path, recursive: bool) -> str:
    if not path.is_dir():
        return f"Not a directory: {path}"
    entries = []
    if recursive:
        for item in sorted(path.rglob("*")):
            rel = item.relative_to(path)
            kind = "d" if item.is_dir() else "f"
            size = item.stat().st_size if item.is_file() else 0
            entries.append(f"[{kind}] {rel} ({size:,} bytes)" if kind == "f" else f"[{kind}] {rel}/")
    else:
        for item in sorted(path.iterdir()):
            kind = "d" if item.is_dir() else "f"
            size = item.stat().st_size if item.is_file() else 0
            entries.append(f"[{kind}] {item.name} ({size:,} bytes)" if kind == "f" else f"[{kind}] {item.name}/")
    return "\n".join(entries) if entries else "(empty directory)"


def _search_files(path: Path, pattern: str) -> str:
    if not path.is_dir():
        path = path.parent
    matches = sorted(path.rglob(pattern))[:100]
    if not matches:
        return f"No files matching '{pattern}'"
    return "\n".join(str(m.relative_to(path)) for m in matches)


def _get_file_info(path: Path) -> str:
    if not path.exists():
        return f"Not found: {path}"
    stat = path.stat()
    kind = "directory" if path.is_dir() else "file"
    from datetime import datetime

    mtime = datetime.fromtimestamp(stat.st_mtime).isoformat()
    return f"Path: {path}\nType: {kind}\nSize: {stat.st_size:,} bytes\nModified: {mtime}"


def _find_in_file(path: Path, query: str, case_sensitive: bool) -> str:
    if not path.is_file():
        return f"File not found: {path}"
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Cannot search: not a text file."

    flags = 0 if case_sensitive else re.IGNORECASE
    matches = []
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(query, line, flags):
            matches.append(f"  {i}: {line.rstrip()}")
            if len(matches) >= 50:
                matches.append("  ... (50 match limit)")
                break

    if not matches:
        return f"No matches for '{query}' in {path.name}"
    return f"Found {len(matches)} match(es) in {path.name}:\n" + "\n".join(matches)


def _get_directory_tree(path: Path, max_depth: int) -> str:
    if not path.is_dir():
        return f"Not a directory: {path}"
    lines = [str(path.name) + "/"]
    _tree_recurse(path, "", max_depth, 0, lines)
    return "\n".join(lines[:500])


def _tree_recurse(path: Path, prefix: str, max_depth: int, depth: int, lines: list[str]) -> None:
    if depth >= max_depth:
        return
    items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for i, item in enumerate(items):
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        name = item.name + "/" if item.is_dir() else item.name
        lines.append(f"{prefix}{connector}{name}")
        if item.is_dir():
            ext = "    " if is_last else "│   "
            _tree_recurse(item, prefix + ext, max_depth, depth + 1, lines)


def _write_file(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Written {len(content)} characters to {path}"
