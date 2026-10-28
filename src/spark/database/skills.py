"""Skills state persistence. Skill content lives on disk; this is state only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def get_skill_states(db: DatabaseConnection, user_guid: str) -> dict[str, bool]:
    """Global enable states keyed by skill name (absent means default on)."""
    ph = db.placeholder
    cur = db.execute(f"SELECT name, enabled FROM skills WHERE user_guid = {ph}", (user_guid,))
    return {name: bool(enabled) for name, enabled in cur.fetchall()}


def set_skill_enabled(db: DatabaseConnection, name: str, enabled: bool, user_guid: str) -> None:
    """Upsert the global enable state for a skill."""
    ph = db.placeholder
    cur = db.execute(
        f"UPDATE skills SET enabled = {ph}, updated_at = CURRENT_TIMESTAMP "
        f"WHERE name = {ph} AND user_guid = {ph}",
        (1 if enabled else 0, name, user_guid),
    )
    if cur.rowcount == 0:
        db.execute(
            f"INSERT INTO skills (name, enabled, user_guid) VALUES ({ph}, {ph}, {ph})",
            (name, 1 if enabled else 0, user_guid),
        )
    db.commit()


def get_conversation_skill_states(db: DatabaseConnection, conversation_id: int) -> dict[str, bool]:
    """Per-conversation overrides keyed by skill name (absent means inherit)."""
    ph = db.placeholder
    cur = db.execute(
        f"SELECT skill_name, enabled FROM conversation_skills WHERE conversation_id = {ph}",
        (conversation_id,),
    )
    return {name: bool(enabled) for name, enabled in cur.fetchall()}


def set_conversation_skill_enabled(
    db: DatabaseConnection, conversation_id: int, skill_name: str, enabled: bool
) -> None:
    """Upsert a per-conversation skill override."""
    ph = db.placeholder
    cur = db.execute(
        f"UPDATE conversation_skills SET enabled = {ph} "
        f"WHERE conversation_id = {ph} AND skill_name = {ph}",
        (1 if enabled else 0, conversation_id, skill_name),
    )
    if cur.rowcount == 0:
        db.execute(
            f"INSERT INTO conversation_skills (conversation_id, skill_name, enabled) "
            f"VALUES ({ph}, {ph}, {ph})",
            (conversation_id, skill_name, 1 if enabled else 0),
        )
    db.commit()


def resolve_enabled(
    db: DatabaseConnection,
    manager: Any,
    user_guid: str,
    conversation_id: int | None = None,
) -> list[dict[str, Any]]:
    """Valid skills that are enabled globally and for the conversation."""
    global_states = get_skill_states(db, user_guid)
    conv_states = (
        get_conversation_skill_states(db, conversation_id) if conversation_id is not None else {}
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
