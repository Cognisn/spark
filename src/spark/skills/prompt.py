"""The Available Skills system-prompt block (progressive disclosure)."""

from __future__ import annotations

from typing import Any


def build_skills_block(skills: list[dict[str, Any]]) -> str:
    """Names and one-line descriptions only; full content loads via use_skill."""
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "When a task matches a skill below, call `use_skill` to load its full "
        "instructions BEFORE attempting the task, and follow them. Use "
        "`read_skill_resource` for a skill's bundled reference files.",
    ]
    lines += [f"- {s['name']}: {s['description']}" for s in skills]
    return "\n".join(lines)
