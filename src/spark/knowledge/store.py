"""Knowledge graph storage: nodes, edges, and build watermarks.

Scopes are exactly "global" or "conv:<conversation_id>". Merge semantics:
an entity re-extracted into the same scope (same lowercased name) merges,
and a repeated relationship bumps the edge weight rather than duplicating.

These are async repository functions over :class:`spark.db.Executor`; the
synchronous builder and query code reach them through :class:`spark.db.SyncDatabase`.
Nodes and edges are unique on their natural keys (revision vector_store_2), and
the upserts are single ``INSERT ... ON CONFLICT`` statements on those keys, so
concurrent first inserts merge instead of duplicating. The watermark's
read-then-write runs in one transaction.
"""

from __future__ import annotations

from typing import Any

from spark.db.dialect import upsert
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
# Merge on (user, scope, lowercased name): weight +1, newer description and embedding if given.
_UPSERT_NODE = (
    upsert(
        "kg_nodes",
        ("scope", "name", "name_key", "entity_type", "description", "embedding", "user_guid"),
        ("user_guid", "scope", "name_key"),
        update=(),
        extra_set=(
            "weight = kg_nodes.weight + 1",
            "updated_at = CURRENT_TIMESTAMP",
            "description = CASE WHEN excluded.description IS NOT NULL "
            "AND excluded.description <> '' THEN excluded.description "
            "ELSE kg_nodes.description END",
            "embedding = COALESCE(excluded.embedding, kg_nodes.embedding)",
        ),
    )
    + " RETURNING id"
)
# Merge on (user, scope, source, target, lowercased relation): weight +1.
_UPSERT_EDGE = (
    upsert(
        "kg_edges",
        (
            "scope",
            "source_node_id",
            "target_node_id",
            "relation",
            "relation_key",
            "description",
            "user_guid",
        ),
        ("user_guid", "scope", "source_node_id", "target_node_id", "relation_key"),
        update=(),
        extra_set=("weight = kg_edges.weight + 1",),
    )
    + " RETURNING id"
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
    node_id = await db.fetch_value(
        _UPSERT_NODE,
        (
            scope,
            name.strip(),
            name.strip().lower(),
            entity_type,
            description,
            embedding,
            user_guid,
        ),
    )
    return int(node_id)


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
    edge_id = await db.fetch_value(
        _UPSERT_EDGE,
        (
            scope,
            source_node_id,
            target_node_id,
            relation.strip(),
            relation.strip().lower(),
            description,
            user_guid,
        ),
    )
    return int(edge_id)


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
