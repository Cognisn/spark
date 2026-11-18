"""Approval-gated skill authoring: create_skill and update_skill.

Authoring stays SPARK's own, above conduit. What it writes is checked by conduit's
``SKILL.md`` parser before anything reaches the disk, so an authored skill is always one
the model can load; resource paths follow conduit's path rule.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from conduit.skills import SkillInvalid, parse_skill_md
from conduit.skills.format import resource_path_refusal

from spark.skills.manager import NAME_RE, get_skills_manager, refusal_text

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


def _escapes_skill(raw_path: str) -> bool:
    """True when a resource path could leave the skill folder on any platform.

    Judged under Windows rules, the stricter of the two (both slashes separate
    and drives exist), so a rooted path, a drive or drive-relative path, a UNC
    path, or a parent step in either separator style is refused wherever SPARK runs.
    """
    if not raw_path.strip():
        return True
    parsed = PureWindowsPath(raw_path)
    return bool(parsed.drive or parsed.root or ".." in parsed.parts)


def _quoted(value: str) -> str:
    """``value`` as a double-quoted front matter value (conduit's parser reads it back)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def skill_md(name: str, description: str, instructions: str) -> str:
    """The SKILL.md an authored skill is written as."""
    return (
        f"---\nname: {name}\ndescription: {_quoted(description.strip())}\n---\n\n"
        f"{instructions.strip()}\n"
    )


def _validate(name: str, description: str, instructions: str, resources: list[dict]) -> str | None:
    """Return an error message, or None when the input is acceptable."""
    if not NAME_RE.match(name) or len(name) > 64:
        return "Skill name must be kebab-case (letters, digits, hyphens), max 64 chars"
    if not description.strip() or len(description) > 1024:
        return "A description of 1 to 1024 characters is required"
    if not instructions.strip():
        return "Instructions must not be empty"
    try:
        parse_skill_md(skill_md(name, description, instructions), expected_name=name)
    except SkillInvalid as refused:
        return f"The skill is not valid: {refusal_text(refused, name)}"
    total = len(instructions.encode("utf-8"))
    for res in resources:
        raw_path = str(res.get("path", ""))
        path = Path(raw_path)
        if _escapes_skill(raw_path):
            return f"Resource path '{res.get('path')}' must be relative, inside the skill"
        if resource_path_refusal(raw_path) is not None:
            return (
                f"Resource path '{res.get('path')}' must be a relative path of letters, "
                "digits, '_', '.' and '-' segments separated by '/'"
            )
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
            skill_md(name, description, instructions), encoding="utf-8", newline="\n"
        )
        for res in resources:
            dest = tmp / Path(str(res["path"]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(res["content"], encoding="utf-8", newline="\n")
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
