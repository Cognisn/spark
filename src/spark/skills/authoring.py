"""Approval-gated skill authoring: create_skill and update_skill."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from spark.skills.manager import NAME_RE, get_skills_manager

logger = logging.getLogger(__name__)

FILE_MAX = 256 * 1024
SKILL_TOTAL_MAX = 2 * 1024 * 1024

_COMMON_PROPS = {
    "name": {"type": "string", "description": "kebab-case skill name"},
    "description": {
        "type": "string",
        "description": "One-line trigger description shown in every system prompt",
    },
    "instructions": {"type": "string", "description": "Full SKILL.md body in markdown"},
    "resources": {
        "type": "array",
        "description": "Optional bundled text files",
        "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}

_CREATE = {
    "name": "create_skill",
    "description": (
        "Create a new user skill (folder with SKILL.md and optional resources). "
        "The user is always asked to approve this action."
    ),
    "inputSchema": {
        "type": "object",
        "properties": _COMMON_PROPS,
        "required": ["name", "description", "instructions"],
    },
}

_UPDATE = {
    "name": "update_skill",
    "description": (
        "Replace an existing USER skill's contents. Bundled skills cannot be "
        "updated. The user is always asked to approve this action."
    ),
    "inputSchema": _CREATE["inputSchema"],
}


def get_write_tools() -> list[dict[str, Any]]:
    return [dict(_CREATE), dict(_UPDATE)]


def _validate(name: str, description: str, instructions: str, resources: list[dict]) -> str | None:
    """Return an error message, or None when the input is acceptable."""
    if not NAME_RE.match(name) or len(name) > 64:
        return "Skill name must be kebab-case (letters, digits, hyphens), max 64 chars"
    if not description.strip() or len(description) > 1024:
        return "A description of 1 to 1024 characters is required"
    if not instructions.strip():
        return "Instructions must not be empty"
    total = len(instructions.encode("utf-8"))
    for res in resources:
        path = Path(str(res.get("path", "")))
        if path.is_absolute() or ".." in path.parts or not str(path).strip():
            return f"Resource path '{res.get('path')}' must be relative, inside the skill"
        content = res.get("content")
        if not isinstance(content, str):
            return f"Resource '{path}' content must be text"
        size = len(content.encode("utf-8"))
        if size > FILE_MAX:
            return f"Resource '{path}' exceeds the 256KB per-file cap"
        total += size
    if total > SKILL_TOTAL_MAX:
        return "Skill exceeds the 2MB total size cap"
    return None


def write_skill(
    name: str,
    description: str,
    instructions: str,
    resources: list[dict],
    *,
    overwrite: bool,
) -> str:
    """Write the skill folder atomically and reload the manager."""
    manager = get_skills_manager()
    target = manager.user_dir / name
    tmp = manager.user_dir / f".tmp-{name}-{uuid.uuid4().hex[:6]}"
    tmp.mkdir(parents=True)
    try:
        (tmp / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n"
            f"{instructions.strip()}\n",
            encoding="utf-8",
        )
        for res in resources:
            dest = tmp / Path(str(res["path"]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(res["content"], encoding="utf-8")
        backup = None
        if target.exists():
            if not overwrite:
                raise FileExistsError(name)
            backup = manager.user_dir / f".bak-{name}-{uuid.uuid4().hex[:6]}"
            target.rename(backup)
        tmp.rename(target)
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    manager.reload()
    return str(target)


def execute_write(tool_name: str, tool_input: dict) -> tuple[str, bool]:
    """Dispatch create_skill or update_skill. Returns (result_text, is_error)."""
    manager = get_skills_manager()
    name = str(tool_input.get("name", "")).strip()
    description = str(tool_input.get("description", ""))
    instructions = str(tool_input.get("instructions", ""))
    resources = tool_input.get("resources") or []
    if not isinstance(resources, list):
        return "resources must be a list of {path, content} items", True

    error = _validate(name, description, instructions, resources)
    if error:
        return error, True

    if tool_name == "create_skill":
        if manager.exists(name) or (manager.user_dir / name).exists():
            return f"A skill named '{name}' already exists; choose another name", True
        path = write_skill(name, description, instructions, resources, overwrite=False)
        return f"Skill '{name}' created at {path} and is now available.", False

    # update_skill: user skills only
    existing = manager.get_skill(name)
    if not existing or existing["source"] != "user":
        return (
            f"'{name}' is not an existing user skill (bundled skills cannot be updated)",
            True,
        )
    path = write_skill(name, description, instructions, resources, overwrite=True)
    return f"Skill '{name}' updated at {path}.", False
