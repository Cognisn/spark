"""Memory tools — store, query, update, and delete persistent memories."""

from __future__ import annotations

from typing import Any

TOOLS = [
    {
        "name": "store_memory",
        "description": "Store information to persistent memory for future conversations. Use this to remember facts, preferences, or instructions the user shares.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The information to remember."},
                "category": {
                    "type": "string",
                    "enum": ["preferences", "facts", "projects", "instructions", "relationships"],
                    "description": "Category of the memory.",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance score from 0.0 to 1.0. Default: 0.5.",
                },
            },
            "required": ["content", "category"],
        },
    },
    {
        "name": "query_memory",
        "description": "Search persistent memories for relevant information. Use this to recall facts, preferences, or context from previous conversations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "category": {
                    "type": "string",
                    "enum": ["preferences", "facts", "projects", "instructions", "relationships"],
                    "description": "Optional category filter.",
                },
                "top_k": {"type": "integer", "description": "Max results. Default: 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_memories",
        "description": "List all stored memories, optionally filtered by category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["preferences", "facts", "projects", "instructions", "relationships"],
                    "description": "Optional category filter.",
                },
            },
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete a specific memory by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "ID of the memory to delete."},
            },
            "required": ["memory_id"],
        },
    },
]


def execute(
    tool_name: str,
    tool_input: dict[str, Any],
    memory_index: Any,
) -> str:
    """Execute a memory tool."""
    if memory_index is None:
        return "Memory system is not available."

    if tool_name == "store_memory":
        content = tool_input.get("content", "").strip()
        if not content:
            return "Error: content is required."
        category = tool_input.get("category", "facts")
        importance = float(tool_input.get("importance", 0.5))
        mid = memory_index.store(content, category, importance=importance)
        if mid is None:
            return "This information is already stored in memory."
        return f"Stored in memory (ID: {mid}, category: {category})."

    elif tool_name == "query_memory":
        query = tool_input.get("query", "")
        category = tool_input.get("category")
        top_k = int(tool_input.get("top_k", 5))
        categories = [category] if category else None
        results = memory_index.search(query, top_k=top_k, categories=categories, threshold=0.3)
        if not results:
            return "No relevant memories found."
        lines = [f"Found {len(results)} relevant memories:\n"]
        for r in results:
            sim = r.get("similarity", 0)
            lines.append(
                f"- [{r.get('category', '?')}] (ID:{r.get('id', '?')}, relevance:{sim:.2f}) {r.get('content', '')}"
            )
        return "\n".join(lines)

    elif tool_name == "list_memories":
        category = tool_input.get("category")
        memories = memory_index.list_all(category=category, limit=50)
        if not memories:
            return "No memories stored." + (f" (category: {category})" if category else "")
        lines = [f"{len(memories)} memories:\n"]
        for m in memories:
            lines.append(
                f"- [{m.get('category', '?')}] (ID:{m.get('id', '?')}) {m.get('content', '')}"
            )
        return "\n".join(lines)

    elif tool_name == "delete_memory":
        mid = int(tool_input.get("memory_id", 0))
        success = memory_index.delete(mid)
        if success:
            return f"Memory {mid} deleted."
        return f"Memory {mid} not found."

    return f"Unknown memory tool: {tool_name}"
