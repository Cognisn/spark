"""Resolve an explicit /skill-name invocation at the start of a user message."""

from __future__ import annotations

from collections.abc import Set
from typing import Any


def apply_skill_trigger(message: str, enabled_names: Set[str]) -> str:
    """Prefix an invocation instruction when the message starts with /<skill>."""
    if not message.startswith("/"):
        return message
    token = message.split(maxsplit=1)[0][1:]
    if token not in enabled_names:
        return message
    return (
        f"[The user explicitly invoked the skill '{token}'. Call "
        f"use_skill('{token}') now and follow its instructions for this "
        f"request.]\n\n{message}"
    )


def resolve_trigger_for_conversation(
    db: Any, user_guid: str, conversation_id: int | None, message: str
) -> str:
    """Failure-safe wrapper used by the streaming endpoints."""
    try:
        from spark.database import skills as skills_db
        from spark.skills.manager import get_skills_manager

        enabled = skills_db.resolve_enabled(db, get_skills_manager(), user_guid, conversation_id)
        return apply_skill_trigger(message, {s["name"] for s in enabled})
    except Exception:  # noqa: BLE001 - the trigger must never break a message
        return message
