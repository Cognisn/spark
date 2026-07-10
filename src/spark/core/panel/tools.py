"""Tool schemas used by panel agents, and speaking-order normalisation."""

from __future__ import annotations

from typing import Any

from spark.core.debate.tools import validate_exhibits as validate_exhibits  # re-export

_EXHIBIT_CONTENT_MAX = 4000

SUBMIT_CONTRIBUTION_TOOL: dict[str, Any] = {
    "name": "submit_contribution",
    "description": (
        "Submit your contribution for this turn. This ends your turn: only the "
        "contribution text and exhibits you submit here are shown to the "
        "moderator and the other panellists. Attach evidence as discrete "
        "exhibits."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "contribution_markdown": {
                "type": "string",
                "description": "Your full contribution for this turn, in markdown.",
            },
            "exhibits": {
                "type": "array",
                "description": "Evidence items backing this contribution.",
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
        "required": ["contribution_markdown"],
    },
}

SET_PANEL_ORDER_TOOL: dict[str, Any] = {
    "name": "set_speaking_order",
    "description": (
        "Announce the order in which the panellists speak each round, as a "
        "list of panellist roles (for example [\"panellist:2\", \"panellist:1\"])."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "order": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Panellist roles in speaking order.",
            }
        },
        "required": ["order"],
    },
}

REQUEST_NEXT_ROUND_TOOL: dict[str, Any] = {
    "name": "request_next_round",
    "description": "Call for another round of discussion before you synthesise.",
    "inputSchema": {"type": "object", "properties": {}},
}

DELIVER_SYNTHESIS_TOOL: dict[str, Any] = {
    "name": "deliver_synthesis",
    "description": "Declare that you are ready to deliver the closing synthesis.",
    "inputSchema": {"type": "object", "properties": {}},
}


def normalise_order(raw: Any, panellist_roles: list[str]) -> list[str]:
    """Normalise a moderator-proposed speaking order.

    Keeps valid roles in the given order, drops unknowns and duplicates, and
    appends any missing panellists in numeric order. Invalid input falls back
    to the numeric order.
    """

    def _numeric(roles: set[str]) -> list[str]:
        return sorted(roles, key=lambda r: int(r.split(":")[1]))

    if not isinstance(raw, list):
        return _numeric(set(panellist_roles))
    valid = set(panellist_roles)
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in valid and item not in out:
            out.append(item)
    out.extend(_numeric(valid - set(out)))
    return out
