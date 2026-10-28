"""Tool schemas used by debate agents, and exhibit validation."""

from __future__ import annotations

import string
from typing import Any

MEMORY_TOOL_NAMES: frozenset[str] = frozenset(
    {"store_memory", "query_memory", "list_memories", "delete_memory"}
)

_EXHIBIT_CONTENT_MAX = 4000

SUBMIT_ARGUMENT_TOOL: dict[str, Any] = {
    "name": "submit_argument",
    "description": (
        "Submit your argument for this turn. This ends your turn: only the "
        "argument text and exhibits you submit here are shown to the judge "
        "and your opponent. Attach evidence as discrete exhibits."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "argument_markdown": {
                "type": "string",
                "description": "Your full argument for this turn, in markdown.",
            },
            "exhibits": {
                "type": "array",
                "description": "Evidence items backing this argument.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string", "maxLength": _EXHIBIT_CONTENT_MAX},
                        "source": {"type": "string"},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        "required": ["argument_markdown"],
    },
}

SET_SPEAKING_ORDER_TOOL: dict[str, Any] = {
    "name": "set_speaking_order",
    "description": "Announce which side argues first in every round.",
    "inputSchema": {
        "type": "object",
        "properties": {"first_speaker": {"type": "string", "enum": ["pro", "con"]}},
        "required": ["first_speaker"],
    },
}

REQUEST_NEXT_ROUND_TOOL: dict[str, Any] = {
    "name": "request_next_round",
    "description": "Call for another round of argument before you rule.",
    "inputSchema": {"type": "object", "properties": {}},
}

DELIVER_RULING_TOOL: dict[str, Any] = {
    "name": "deliver_ruling",
    "description": "Declare that you are ready to deliver the final judgement.",
    "inputSchema": {"type": "object", "properties": {}},
}


def validate_exhibits(raw: Any) -> list[dict[str, Any]]:
    """Normalise raw exhibit input: relabel A, B, C in order, truncate content."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    labels = string.ascii_uppercase
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = len(out)
        label = labels[idx] if idx < len(labels) else f"X{idx}"
        out.append(
            {
                "label": label,
                "title": str(item.get("title", ""))[:200],
                "content": str(item.get("content", ""))[:_EXHIBIT_CONTENT_MAX],
                "source": str(item.get("source", ""))[:500] or None,
            }
        )
    return out
