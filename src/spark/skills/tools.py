"""Skill tools: load instructions, read resources, and author skills."""

from __future__ import annotations

import logging
from typing import Any

from spark.skills.manager import SkillResourceError, get_skills_manager

logger = logging.getLogger(__name__)

READ_TOOL_NAMES: frozenset[str] = frozenset({"use_skill", "read_skill_resource"})
WRITE_TOOL_NAMES: frozenset[str] = frozenset({"create_skill", "update_skill"})

_USE_SKILL = {
    "name": "use_skill",
    "description": (
        "Load a skill's full instructions. Call this BEFORE attempting any "
        "task that matches a skill in the Available Skills list, then follow "
        "the instructions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"skill_name": {"type": "string"}},
        "required": ["skill_name"],
    },
}

_READ_RESOURCE = {
    "name": "read_skill_resource",
    "description": "Read a text resource file bundled inside a skill's folder.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
            "relative_path": {"type": "string"},
        },
        "required": ["skill_name", "relative_path"],
    },
}


def get_read_tools() -> list[dict[str, Any]]:
    """The two read-only tools (also given to the debate judge)."""
    return [dict(_USE_SKILL), dict(_READ_RESOURCE)]


def get_tools() -> list[dict[str, Any]]:
    """All skills tools offered through the registry."""
    from spark.skills.authoring import get_write_tools

    return get_read_tools() + get_write_tools()


def _allowed_names(config: dict) -> list[str] | None:
    allow = config.get("_skills_allowlist")
    return allow if isinstance(allow, list) else None


def _use_skill(tool_input: dict, config: dict) -> tuple[str, bool]:
    manager = get_skills_manager()
    name = str(tool_input.get("skill_name", "")).strip()
    allow = _allowed_names(config)
    skill = manager.get_skill(name)
    if not skill or (allow is not None and name not in allow):
        names = [s["name"] for s in manager.list_skills()]
        if allow is not None:
            names = [n for n in names if n in allow]
        available = ", ".join(names) or "(none)"
        return f"Unknown skill '{name}'. Available skills: {available}", True
    resources = manager.list_resources(name)
    resource_note = (
        "\n\n## Bundled resources (load with read_skill_resource)\n"
        + "\n".join(f"- {r}" for r in resources)
        if resources
        else ""
    )
    return (
        f"# Skill: {name}\n"
        f"Folder (for run_command script paths): {manager.skill_path(name)}\n\n"
        f"{manager.get_body(name).strip()}{resource_note}"
    ), False


def _read_resource(tool_input: dict, config: dict) -> tuple[str, bool]:
    manager = get_skills_manager()
    name = str(tool_input.get("skill_name", "")).strip()
    allow = _allowed_names(config)
    if allow is not None and name not in allow:
        return f"Unknown skill '{name}'.", True
    try:
        return (
            manager.read_resource(name, str(tool_input.get("relative_path", "")).strip()),
            False,
        )
    except SkillResourceError as e:
        return str(e), True


def execute(tool_name: str, tool_input: dict, config: dict) -> tuple[str, bool]:
    """Dispatch a skills tool call. Returns (result_text, is_error)."""
    if tool_name == "use_skill":
        return _use_skill(tool_input, config)
    if tool_name == "read_skill_resource":
        return _read_resource(tool_input, config)
    if tool_name in WRITE_TOOL_NAMES:
        from spark.skills.authoring import execute_write

        return execute_write(tool_name, tool_input)
    return f"Unknown skills tool: {tool_name}", True
