"""AI-assisted autonomous action creation tools and system prompt."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTION_CREATION_SYSTEM_PROMPT = """You are an AI assistant helping the user create an autonomous action in Spark.

Guide the user through these steps:
1. Understand what the user wants the action to do
2. Gather: description, action prompt, and schedule
3. Ask clarifying questions if anything is unclear
4. Use `validate_schedule` to check the schedule expression
5. Generate a clear action prompt based on what the user described
6. Present a summary and ask: "Shall I create this action? Reply **yes** or **no**."
7. When the user says yes/confirm/proceed/create — **immediately call `create_autonomous_action`**. Do NOT ask again. Do NOT re-summarise. Just call the tool.

**CRITICAL: When the user confirms, you MUST call the `create_autonomous_action` tool in your very next response. Do not ask for confirmation again.**

**Schedule types:**
- **One-off**: Runs once at a specific date/time. Format: "YYYY-MM-DD HH:MM"
- **Recurring**: Runs on a cron schedule. Format: 5-field cron "minute hour day month day_of_week"
  Examples: "0 8 * * 1-5" = Weekdays at 8am, "0 */6 * * *" = Every 6 hours, "30 9 1 * *" = 1st of month at 9:30am

**Defaults:**
- Context mode: fresh (start clean each run)
- Max failures before disable: 3
- Max tokens: 8192

**Tools available to you:**
- `list_available_tools` — show the user what tools the action can use
- `validate_schedule` — check a schedule expression is valid
- `create_autonomous_action` — create the action. Call this as soon as the user confirms.

If the user says "cancel", stop the creation process immediately.
Always explain cron expressions in plain language when presenting them.
"""

CREATION_TOOLS = [
    {
        "name": "list_available_tools",
        "description": "List all tools available for autonomous actions (builtin + MCP).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "validate_schedule",
        "description": "Validate a schedule expression (one-off datetime or cron).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schedule_type": {
                    "type": "string",
                    "enum": ["one_off", "recurring"],
                    "description": "Type of schedule.",
                },
                "schedule_value": {
                    "type": "string",
                    "description": "The schedule value: 'YYYY-MM-DD HH:MM' for one-off, or 5-field cron for recurring.",
                },
            },
            "required": ["schedule_type", "schedule_value"],
        },
    },
    {
        "name": "create_autonomous_action",
        "description": "Create the autonomous action. Only call after the user confirms the summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Action name (unique)."},
                "description": {"type": "string", "description": "Brief description."},
                "action_prompt": {
                    "type": "string",
                    "description": "The full prompt for the AI to execute.",
                },
                "model_id": {"type": "string", "description": "Model to use."},
                "schedule_type": {"type": "string", "enum": ["one_off", "recurring"]},
                "schedule_value": {"type": "string", "description": "Schedule expression."},
                "context_mode": {
                    "type": "string",
                    "enum": ["fresh", "cumulative"],
                    "description": "Default: fresh.",
                },
                "max_failures": {"type": "integer", "description": "Default: 3."},
                "max_tokens": {"type": "integer", "description": "Default: 8192."},
            },
            "required": ["name", "action_prompt", "model_id", "schedule_type", "schedule_value"],
        },
    },
]


def execute_creation_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    config: dict[str, Any],
    db_connection: Any,
) -> str:
    """Execute one of the creation tools. Returns result text."""
    if tool_name == "list_available_tools":
        return _list_available_tools(config)
    elif tool_name == "validate_schedule":
        return _validate_schedule(tool_input)
    elif tool_name == "create_autonomous_action":
        return _create_action(tool_input, db_connection, config)
    return f"Unknown tool: {tool_name}"


def _list_available_tools(config: dict[str, Any]) -> str:
    """List all available tools."""
    from spark.tools.registry import get_builtin_tools

    tools = get_builtin_tools(config)
    lines = ["Available tools:\n"]
    for t in tools:
        lines.append(f"- **{t['name']}**: {t.get('description', '')}")
    return "\n".join(lines) if len(lines) > 1 else "No tools available."


def _validate_schedule(tool_input: dict[str, Any]) -> str:
    """Validate a schedule expression."""
    schedule_type = tool_input.get("schedule_type", "")
    value = tool_input.get("schedule_value", "").strip()

    if schedule_type == "one_off":
        return _validate_one_off(value)
    elif schedule_type == "recurring":
        return _validate_recurring(value)
    return "Invalid schedule_type. Use 'one_off' or 'recurring'."


def _validate_one_off(value: str) -> str:
    """Validate a one-off datetime."""
    formats = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt < datetime.now():
                return f"Invalid: {value} is in the past."
            human = dt.strftime("%A, %d %B %Y at %H:%M")
            return f"Valid one-off schedule: {human}"
        except ValueError:
            continue
    return f"Invalid datetime format: '{value}'. Use YYYY-MM-DD HH:MM."


def _validate_recurring(value: str) -> str:
    """Validate a cron expression."""
    parts = value.split()
    if len(parts) != 5:
        return f"Invalid cron: expected 5 fields (minute hour day month day_of_week), got {len(parts)}."
    try:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )
        next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        human = _cron_to_human(parts)
        next_str = next_fire.strftime("%A, %d %B %Y at %H:%M UTC") if next_fire else "N/A"
        return f"Valid recurring schedule: {human}\nNext run: {next_str}"
    except Exception as e:
        return f"Invalid cron expression: {e}"


def _cron_to_human(parts: list[str]) -> str:
    """Convert cron fields to human-readable description."""
    minute, hour, day, month, dow = parts

    time_str = f"{hour.zfill(2)}:{minute.zfill(2)}" if hour != "*" and minute != "*" else ""

    dow_map = {
        "1-5": "Weekdays",
        "0-6": "Every day",
        "MON-FRI": "Weekdays",
        "0": "Sunday",
        "1": "Monday",
        "2": "Tuesday",
        "3": "Wednesday",
        "4": "Thursday",
        "5": "Friday",
        "6": "Saturday",
        "MON": "Monday",
        "TUE": "Tuesday",
        "WED": "Wednesday",
        "THU": "Thursday",
        "FRI": "Friday",
        "SAT": "Saturday",
        "SUN": "Sunday",
    }

    if dow != "*" and dow in dow_map:
        when = dow_map[dow]
        return f"{when} at {time_str}" if time_str else when

    if "*/" in minute:
        interval = minute.split("/")[1]
        return f"Every {interval} minutes"
    if "*/" in hour:
        interval = hour.split("/")[1]
        return f"Every {interval} hours"

    if day != "*":
        suffix = (
            "th"
            if day not in ("1", "2", "3", "21", "22", "23", "31")
            else {"1": "st", "2": "nd", "3": "rd"}.get(day[-1], "th")
        )
        return (
            f"{day}{suffix} of every month at {time_str}"
            if time_str
            else f"{day}{suffix} of every month"
        )

    return f"At {time_str}" if time_str else " ".join(parts)


def _create_action(
    tool_input: dict[str, Any], db_connection: Any, config: dict[str, Any] | None = None
) -> str:
    """Create the action in the database."""
    from spark.database import autonomous_actions

    config = config or {}

    name = tool_input.get("name", "").strip()
    if not name:
        return "Error: name is required."

    prompt = tool_input.get("action_prompt", "").strip()
    if not prompt:
        return "Error: action_prompt is required."

    model_id = tool_input.get("model_id", "").strip()
    if not model_id:
        return "Error: model_id is required."

    schedule_type = tool_input.get("schedule_type", "one_off")
    schedule_value = tool_input.get("schedule_value", "")

    # Build schedule config
    if schedule_type == "one_off":
        schedule_config = json.dumps({"run_at": schedule_value})
    else:
        schedule_config = json.dumps({"cron": schedule_value})

    try:
        aid = autonomous_actions.create_action(
            db_connection,
            name=name,
            action_prompt=prompt,
            model_id=model_id,
            user_guid=config.get("_user_guid", "default"),
            description=tool_input.get("description", ""),
            schedule_type=schedule_type,
            schedule_config=schedule_config,
            context_mode=tool_input.get("context_mode", "fresh"),
            max_failures=int(tool_input.get("max_failures", 3)),
            max_tokens=int(tool_input.get("max_tokens", 8192)),
        )
        return f"Action '{name}' created successfully (ID: {aid}). It has been scheduled and will run according to the configured schedule."
    except Exception as e:
        return f"Error creating action: {e}"
