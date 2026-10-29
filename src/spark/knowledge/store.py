"""Knowledge graph storage: nodes, edges, and build watermarks.

Scopes are exactly "global" or "conv:<conversation_id>". Merge semantics:
an entity re-extracted into the same scope (same lowercased name) merges,
and a repeated relationship bumps the edge weight rather than duplicating.

These are async repository functions over :class:`spark.db.Executor`; the
synchronous builder and query code reach them through :class:`spark.db.SyncDatabase`.
Each read-then-write (the upserts and the watermark) runs in one transaction.
"""

from __future__ import annotations

from typing import Any

from spark.db.engine import Executor

__all__ = [
    "clear_scope",
    "count_edges",
    "count_nodes",
    "get_build_state",
    "get_edges",
    "get_edges_for_nodes",
    "get_nodes",
    "get_nodes_by_ids",
    "set_watermark",
    "upsert_edge",
    "upsert_node",
]

_NODE_COLUMNS = (
    "id",
    "scope",
    "name",
    "name_key",
    "entity_type",
    "description",
    "embedding",
    "weight",
    "user_guid",
    "created_at",
    "updated_at",
)
_EDGE_COLUMNS = (
    "id",
    "scope",
    "source_node_id",
    "target_node_id",
    "relation",
    "description",
    "weight",
    "user_guid",
    "created_at",
)


async def upsert_node(
    db: Executor,
    scope: str,
    name: str,
    entity_type: str,
    description: str,
    embedding: bytes | None,
    user_guid: str,
) -> int:
    """Create or merge a node keyed by (scope, lowercased name). Returns node id."""
    name_key = name.strip().lower()
    async with db.transaction() as conn:
        node_id = await conn.fetch_value(
            "SELECT id FROM kg_nodes WHERE scope = ? AND name_key = ? AND user_guid = ?",
            (scope, name_key, user_guid),
        )
        if node_id is not None:
            sets = ["weight = weight + 1", "updated_at = CURRENT_TIMESTAMP"]
            params: list[Any] = []
            if description:
                sets.append("description = ?")
                params.append(description)
            if embedding is not None:
                sets.append("embedding = ?")
                params.append(embedding)
            params.append(node_id)
            await conn.execute(f"UPDATE kg_nodes SET {', '.join(sets)} WHERE id = ?", tuple(params))
            return int(node_id)
        new_id = await conn.fetch_value(
            """INSERT INTO kg_nodes (scope, name, name_key, entity_type, description,
                                     embedding, user_guid)
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (scope, name.strip(), name_key, entity_type, description, embedding, user_guid),
        )
    return int(new_id)


async def upsert_edge(
    db: Executor,
    scope: str,
    source_node_id: int,
    target_node_id: int,
    relation: str,
    description: str,
    user_guid: str,
) -> int:
    """Create or merge an edge keyed by (scope, source, target, relation). Returns id."""
    async with db.transaction() as conn:
        edge_id = await conn.fetch_value(
            """SELECT id FROM kg_edges
                WHERE scope = ? AND source_node_id = ? AND target_node_id = ?
                  AND LOWER(relation) = ? AND user_guid = ?""",
            (scope, source_node_id, target_node_id, relation.strip().lower(), user_guid),
        )
        if edge_id is not None:
            await conn.execute("UPDATE kg_edges SET weight = weight + 1 WHERE id = ?", (edge_id,))
            return int(edge_id)
        new_id = await conn.fetch_value(
            """INSERT INTO kg_edges (scope, source_node_id, target_node_id, relation,
                                     description, user_guid)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (scope, source_node_id, target_node_id, relation.strip(), description, user_guid),
        )
    return int(new_id)


async def get_nodes(
    db: Executor, scope: str, user_guid: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Nodes in a scope, heaviest first."""
    sql = (
        f"SELECT {', '.join(_NODE_COLUMNS)} FROM kg_nodes "
        "WHERE scope = ? AND user_guid = ? ORDER BY weight DESC, id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return await db.fetch_all(sql, (scope, user_guid))


async def get_edges(db: Executor, scope: str, user_guid: str) -> list[dict[str, Any]]:
    """All edges in a scope."""
    return await db.fetch_all(
        f"SELECT {', '.join(_EDGE_COLUMNS)} FROM kg_edges "
        "WHERE scope = ? AND user_guid = ? ORDER BY weight DESC, id",
        (scope, user_guid),
    )


async def get_edges_for_nodes(
    db: Executor, scope: str, node_ids: list[int], user_guid: str
) -> list[dict[str, Any]]:
    """Edges in a scope touching any of the given nodes."""
    if not node_ids:
        return []
    marks = ", ".join("?" for _ in node_ids)
    return await db.fetch_all(
        f"SELECT {', '.join(_EDGE_COLUMNS)} FROM kg_edges "
        "WHERE scope = ? AND user_guid = ? "
        f"AND (source_node_id IN ({marks}) OR target_node_id IN ({marks})) "
        "ORDER BY weight DESC, id",
        (scope, user_guid, *node_ids, *node_ids),
    )


async def get_nodes_by_ids(
    db: Executor, scope: str, node_ids: list[int], user_guid: str
) -> list[dict[str, Any]]:
    """Specific nodes in a scope by id."""
    if not node_ids:
        return []
    marks = ", ".join("?" for _ in node_ids)
    return await db.fetch_all(
        f"SELECT {', '.join(_NODE_COLUMNS)} FROM kg_nodes "
        f"WHERE scope = ? AND user_guid = ? AND id IN ({marks})",
        (scope, user_guid, *node_ids),
    )


async def count_nodes(db: Executor, scope: str, user_guid: str) -> int:
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM kg_nodes WHERE scope = ? AND user_guid = ?", (scope, user_guid)
    )
    return int(count)


async def count_edges(db: Executor, scope: str, user_guid: str) -> int:
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM kg_edges WHERE scope = ? AND user_guid = ?", (scope, user_guid)
    )
    return int(count)


async def clear_scope(db: Executor, scope: str, user_guid: str) -> None:
    """Remove a scope's nodes, edges, and build state, in one transaction."""
    async with db.transaction() as conn:
        for table in ("kg_edges", "kg_nodes", "kg_builds"):
            await conn.execute(
                f"DELETE FROM {table} WHERE scope = ? AND user_guid = ?", (scope, user_guid)
            )


async def get_build_state(db: Executor, scope: str, user_guid: str) -> list[dict[str, Any]]:
    """Watermark rows for a scope."""
    return await db.fetch_all(
        """SELECT id, scope, source_key, watermark, built_at, status, error
            FROM kg_builds WHERE scope = ? AND user_guid = ? ORDER BY source_key""",
        (scope, user_guid),
    )


async def set_watermark(
    db: Executor,
    scope: str,
    source_key: str,
    watermark: int,
    user_guid: str,
    *,
    status: str = "complete",
    error: str | None = None,
) -> None:
    """Set the watermark row for (scope, source_key): update, else insert, in one transaction."""
    async with db.transaction() as conn:
        changed = await conn.execute(
            """UPDATE kg_builds
                SET watermark = ?, built_at = CURRENT_TIMESTAMP, status = ?, error = ?
                WHERE scope = ? AND source_key = ? AND user_guid = ?""",
            (watermark, status, error, scope, source_key, user_guid),
        )
        if changed == 0:
            await conn.execute(
                """INSERT INTO kg_builds (scope, source_key, watermark, built_at, status,
                                          error, user_guid)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)""",
                (scope, source_key, watermark, status, error, user_guid),
            )
