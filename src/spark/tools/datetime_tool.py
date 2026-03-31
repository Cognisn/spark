"""DateTime tool — get current date and time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

TOOLS = [
    {
        "name": "get_current_datetime",
        "description": "Get the current date and time, optionally in a specific timezone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone (e.g. 'America/New_York', 'Europe/London'). Defaults to UTC.",
                },
                "format": {
                    "type": "string",
                    "enum": ["iso", "human"],
                    "description": "Output format. 'iso' for ISO 8601, 'human' for readable. Default: iso.",
                },
            },
        },
    }
]


def execute(tool_input: dict[str, Any]) -> str:
    """Execute the get_current_datetime tool."""
    tz_name = tool_input.get("timezone", "UTC")
    fmt = tool_input.get("format", "iso")

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"

    now = datetime.now(tz)

    if fmt == "human":
        formatted = now.strftime("%A, %B %d, %Y at %I:%M:%S %p %Z")
    else:
        formatted = now.isoformat()

    return f"Current date/time ({tz_name}): {formatted}"
