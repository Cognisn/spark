"""Skills state persistence. Skill content lives on disk; this is state only."""

from __future__ import annotations

from typing import Any

from spark.db.engine import Executor

__all__ = [
    "get_conversation_skill_states",
    "get_skill_states",
    "resolve_enabled",
    "set_conversation_skill_enabled",
    "set_skill_enabled",
]


async def get_skill_states(db: Executor, user_guid: str) -> dict[str, bool]:
    """Global enable states keyed by skill name (absent means default on)."""
    rows = await db.fetch_all("SELECT name, enabled FROM skills WHERE user_guid = ?", (user_guid,))
    return {r["name"]: bool(r["enabled"]) for r in rows}


async def set_skill_enabled(db: Executor, name: str, enabled: bool, user_guid: str) -> None:
    """Set the global enable state for a skill (update, else insert, in one transaction)."""
    async with db.transaction() as conn:
        changed = await conn.execute(
            "UPDATE skills SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE name = ? AND user_guid = ?",
            (1 if enabled else 0, name, user_guid),
        )
        if changed == 0:
            await conn.execute(
                "INSERT INTO skills (name, enabled, user_guid) VALUES (?, ?, ?)",
                (name, 1 if enabled else 0, user_guid),
            )


async def get_conversation_skill_states(db: Executor, conversation_id: int) -> dict[str, bool]:
    """Per-conversation overrides keyed by skill name (absent means inherit)."""
    rows = await db.fetch_all(
        "SELECT skill_name, enabled FROM conversation_skills WHERE conversation_id = ?",
        (conversation_id,),
    )
    return {r["skill_name"]: bool(r["enabled"]) for r in rows}


async def set_conversation_skill_enabled(
    db: Executor, conversation_id: int, skill_name: str, enabled: bool
) -> None:
    """Set a per-conversation skill override (update, else insert, in one transaction)."""
    async with db.transaction() as conn:
        changed = await conn.execute(
            "UPDATE conversation_skills SET enabled = ? WHERE conversation_id = ? AND skill_name = ?",
            (1 if enabled else 0, conversation_id, skill_name),
        )
        if changed == 0:
            await conn.execute(
                "INSERT INTO conversation_skills (conversation_id, skill_name, enabled) "
                "VALUES (?, ?, ?)",
                (conversation_id, skill_name, 1 if enabled else 0),
            )


async def resolve_enabled(
    db: Executor,
    manager: Any,
    user_guid: str,
    conversation_id: int | None = None,
) -> list[dict[str, Any]]:
    """Valid skills that are enabled globally and for the conversation."""
    global_states = await get_skill_states(db, user_guid)
    conv_states = (
        await get_conversation_skill_states(db, conversation_id)
        if conversation_id is not None
        else {}
    )
    out = []
    for skill in manager.list_skills():
        name = skill["name"]
        if not global_states.get(name, True):
            continue
        if not conv_states.get(name, True):
            continue
        out.append(skill)
    return out
