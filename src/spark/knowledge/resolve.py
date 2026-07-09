"""Query-time scope resolution: which graphs a conversation can see.

Pooling and toggles resolve here at query time, never by copying data, so
linking or unlinking conversations changes graph visibility instantly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spark.knowledge import store

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

_DEFAULTS = {"kg_local_enabled": False, "kg_use_global": True, "kg_auto_context": True}


def conversation_kg_settings(
    db: DatabaseConnection, conversation_id: int, user_guid: str
) -> dict[str, bool]:
    """The three knowledge graph flags for a conversation (defaults when missing)."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT kg_local_enabled, kg_use_global, kg_auto_context
            FROM conversations WHERE id = {ph} AND user_guid = {ph}""",
        (conversation_id, user_guid),
    )
    row = cur.fetchone()
    if not row:
        return dict(_DEFAULTS)
    return {
        "kg_local_enabled": bool(row[0]),
        "kg_use_global": bool(row[1]),
        "kg_auto_context": bool(row[2]),
    }


def available_scopes(
    db: DatabaseConnection, conversation_id: int | None, user_guid: str
) -> list[str]:
    """Scopes visible to a conversation: local, linked locals, then global."""

    def has_nodes(scope: str) -> bool:
        return store.count_nodes(db, scope, user_guid) > 0

    if conversation_id is None:
        return ["global"] if has_nodes("global") else []

    scopes: list[str] = []
    settings = conversation_kg_settings(db, conversation_id, user_guid)

    local_scope = f"conv:{conversation_id}"
    if settings["kg_local_enabled"] and has_nodes(local_scope):
        scopes.append(local_scope)

    try:
        from spark.database import conversation_links

        for linked in conversation_links.get_links(db, conversation_id, user_guid):
            linked_scope = f"conv:{linked['id']}"
            if linked_scope not in scopes and has_nodes(linked_scope):
                scopes.append(linked_scope)
    except Exception:  # noqa: BLE001 - link problems must not break resolution
        pass

    if settings["kg_use_global"] and has_nodes("global"):
        scopes.append("global")

    return scopes
