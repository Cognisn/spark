"""Skills state persistence. Skill content lives on disk; this is state only.

Both tables are unique on their natural keys (revision vector_store_2); the
setters are single upserts on those keys.
"""

from __future__ import annotations

from typing import Any

from spark.database.scope import (
    READABLE_CONVERSATIONS,
    Capability,
    allows,
    conversation_role,
)
from spark.db.dialect import upsert
from spark.db.engine import Executor

__all__ = [
    "SETTING_DISABLED_BUILTIN",
    "disabled_builtin",
    "get_conversation_skill_states",
    "get_skill_states",
    "resolve_enabled",
    "set_conversation_skill_enabled",
    "set_skill_enabled",
]

#: The instance setting naming the built-in skills switched off for every user.
SETTING_DISABLED_BUILTIN = "skills.disabled_builtin"


def disabled_builtin() -> frozenset[str]:
    """The built-in skills the administrator has switched off (none when no settings
    source is configured, as in tools and tests that act for no instance)."""
    from spark.instance_settings import accessor

    try:
        value = accessor.get(SETTING_DISABLED_BUILTIN)
    except RuntimeError:
        return frozenset()
    return frozenset(str(name) for name in value or ())


_SET_SKILL = upsert(
    "skills",
    ("name", "enabled", "user_guid"),
    ("user_guid", "name"),
    update=("enabled",),
    extra_set=("updated_at = CURRENT_TIMESTAMP",),
)
_SET_CONVERSATION_SKILL = upsert(
    "conversation_skills",
    ("conversation_id", "skill_name", "enabled"),
    ("conversation_id", "skill_name"),
    update=("enabled",),
)


async def get_skill_states(db: Executor, user_guid: str) -> dict[str, bool]:
    """Global enable states keyed by skill name (absent means default on)."""
    rows = await db.fetch_all("SELECT name, enabled FROM skills WHERE user_guid = ?", (user_guid,))
    return {r["name"]: bool(r["enabled"]) for r in rows}


async def set_skill_enabled(db: Executor, name: str, enabled: bool, user_guid: str) -> None:
    """Set the global enable state for a skill."""
    await db.execute(_SET_SKILL, (name, 1 if enabled else 0, user_guid))


async def get_conversation_skill_states(
    db: Executor, user_guid: str, conversation_id: int
) -> dict[str, bool]:
    """Overrides on a conversation the user may read, keyed by skill name (absent: inherit).

    The overrides are the owner's settings; every participant's turn follows them.
    """
    rows = await db.fetch_all(
        f"""SELECT skill_name, enabled FROM conversation_skills WHERE conversation_id = ?
            AND conversation_id IN ({READABLE_CONVERSATIONS})""",
        (conversation_id, user_guid),
    )
    return {r["skill_name"]: bool(r["enabled"]) for r in rows}


async def set_conversation_skill_enabled(
    db: Executor, user_guid: str, conversation_id: int, skill_name: str, enabled: bool
) -> None:
    """Set a skill override on a conversation the user owns (nothing otherwise)."""
    async with db.transaction() as conn:
        role = await conversation_role(conn, user_guid, conversation_id)
        if allows(role, Capability.MANAGE):
            await conn.execute(
                _SET_CONVERSATION_SKILL, (conversation_id, skill_name, 1 if enabled else 0)
            )


async def resolve_enabled(
    db: Executor,
    manager: Any,
    user_guid: str,
    conversation_id: int | None = None,
) -> list[dict[str, Any]]:
    """Valid skills that are enabled globally and for the conversation.

    A built-in (bundled) skill the administrator has switched off for the instance
    (``skills.disabled_builtin``) is never offered, whatever the user chose.
    """
    switched_off = disabled_builtin()
    global_states = await get_skill_states(db, user_guid)
    conv_states = (
        await get_conversation_skill_states(db, user_guid, conversation_id)
        if conversation_id is not None
        else {}
    )
    out = []
    for skill in manager.list_skills():
        name = skill["name"]
        if skill.get("source") == "bundled" and name in switched_off:
            continue
        if not global_states.get(name, True):
            continue
        if not conv_states.get(name, True):
            continue
        out.append(skill)
    return out
