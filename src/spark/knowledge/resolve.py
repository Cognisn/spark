"""Query-time scope resolution: which graphs a conversation can see.

Pooling and toggles resolve here at query time, never by copying data, so
linking or unlinking conversations changes graph visibility instantly.
"""

from __future__ import annotations

from spark.db.engine import Executor
from spark.knowledge import store

__all__ = ["available_scopes", "conversation_kg_settings"]

_DEFAULTS = {"kg_local_enabled": False, "kg_use_global": True, "kg_auto_context": True}


async def conversation_kg_settings(
    db: Executor, conversation_id: int, user_guid: str
) -> dict[str, bool]:
    """The three knowledge graph flags for a conversation (defaults when missing)."""
    row = await db.fetch_one(
        """SELECT kg_local_enabled, kg_use_global, kg_auto_context
            FROM conversations WHERE id = ? AND user_guid = ?""",
        (conversation_id, user_guid),
    )
    if row is None:
        return dict(_DEFAULTS)
    return {key: bool(row[key]) for key in _DEFAULTS}


async def available_scopes(db: Executor, conversation_id: int | None, user_guid: str) -> list[str]:
    """Scopes visible to a conversation: local, linked locals, then global."""

    async def has_nodes(scope: str) -> bool:
        return await store.count_nodes(db, scope, user_guid) > 0

    if conversation_id is None:
        return ["global"] if await has_nodes("global") else []

    scopes: list[str] = []
    settings = await conversation_kg_settings(db, conversation_id, user_guid)

    local_scope = f"conv:{conversation_id}"
    if settings["kg_local_enabled"] and await has_nodes(local_scope):
        scopes.append(local_scope)

    try:
        from spark.database import conversation_links

        for linked in await conversation_links.get_links(db, conversation_id, user_guid):
            linked_scope = f"conv:{linked['id']}"
            if linked_scope not in scopes and await has_nodes(linked_scope):
                scopes.append(linked_scope)
    except Exception:  # noqa: BLE001 - link problems must not break resolution
        pass

    if settings["kg_use_global"] and await has_nodes("global"):
        scopes.append("global")

    return scopes
