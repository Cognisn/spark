"""Intelligent tool selection to reduce token usage."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Category → name patterns
_CATEGORIES: dict[str, list[str]] = {
    "filesystem": ["read_file", "write_file", "list_dir", "search_files", "create_dir"],
    "documents": ["read_doc", "read_pdf", "read_excel", "read_pptx", "create_doc"],
    "web": ["web_search", "web_extract", "fetch_url"],
    "datetime": ["get_current_datetime"],
    "archives": ["extract_archive", "list_archive"],
}

# Category → keywords that trigger inclusion
_KEYWORDS: dict[str, set[str]] = {
    "filesystem": {"file", "read", "write", "directory", "folder", "path", "save", "create file"},
    "documents": {"document", "pdf", "word", "excel", "spreadsheet", "powerpoint", "docx", "xlsx"},
    "web": {"search", "web", "url", "website", "internet", "browse", "fetch", "http"},
    "datetime": {"time", "date", "today", "now", "timezone", "clock"},
    "archives": {"zip", "tar", "archive", "extract", "compress"},
}


class ToolSelector:
    """Selects a relevant subset of tools based on the user's message."""

    def __init__(self, max_tools: int = 30) -> None:
        self._max_tools = max_tools

    def select_tools(
        self,
        all_tools: list[dict[str, Any]],
        user_message: str,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Select relevant tools for the current request.

        Returns up to max_tools tools, prioritising those matching
        detected categories from the user message and recent history.
        """
        if len(all_tools) <= self._max_tools:
            return all_tools

        # Detect relevant categories
        categories = self._detect_categories(user_message, conversation_history)

        selected: list[dict[str, Any]] = []
        selected_names: set[str] = set()

        if categories:
            # Add tools matching detected categories
            for tool in all_tools:
                name = tool.get("name", "")
                if any(
                    any(pattern in name for pattern in _CATEGORIES.get(cat, []))
                    for cat in categories
                ):
                    if name not in selected_names and len(selected) < self._max_tools:
                        selected.append(tool)
                        selected_names.add(name)

        # Always include built-in utility tools (datetime etc.)
        for tool in all_tools:
            name = tool.get("name", "")
            if name in ("get_current_datetime",) and name not in selected_names:
                if len(selected) < self._max_tools:
                    selected.append(tool)
                    selected_names.add(name)

        # Backfill remaining slots
        for tool in all_tools:
            if len(selected) >= self._max_tools:
                break
            name = tool.get("name", "")
            if name not in selected_names:
                selected.append(tool)
                selected_names.add(name)

        return selected

    def _detect_categories(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> set[str]:
        """Detect relevant tool categories from message and recent history."""
        text = user_message.lower()

        # Include last few messages for context
        if history:
            for msg in history[-5:]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    text += " " + content.lower()

        categories: set[str] = set()
        for category, keywords in _KEYWORDS.items():
            if any(kw in text for kw in keywords):
                categories.add(category)

        return categories
