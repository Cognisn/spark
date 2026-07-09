"""Knowledge graph storage: nodes, edges, and build watermarks.

Scopes are exactly "global" or "conv:<conversation_id>". Merge semantics:
an entity re-extracted into the same scope (same lowercased name) merges,
and a repeated relationship bumps the edge weight rather than duplicating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

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


def upsert_node(
    db: DatabaseConnection,
    scope: str,
    name: str,
    entity_type: str,
    description: str,
    embedding: bytes | None,
    user_guid: str,
) -> int:
    """Create or merge a node keyed by (scope, lowercased name). Returns node id."""
    ph = db.placeholder
    name_key = name.strip().lower()
    cur = db.execute(
        f"SELECT id FROM kg_nodes WHERE scope = {ph} AND name_key = {ph} AND user_guid = {ph}",
        (scope, name_key, user_guid),
    )
    row = cur.fetchone()
    if row:
        node_id = row[0]
        sets = ["weight = weight + 1", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if description:
            sets.append(f"description = {ph}")
            params.append(description)
        if embedding is not None:
            sets.append(f"embedding = {ph}")
            params.append(embedding)
        params.append(node_id)
        db.execute(f"UPDATE kg_nodes SET {', '.join(sets)} WHERE id = {ph}", tuple(params))
        db.commit()
        return node_id
    cur = db.execute(
        f"""INSERT INTO kg_nodes (scope, name, name_key, entity_type, description,
                                  embedding, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (scope, name.strip(), name_key, entity_type, description, embedding, user_guid),
    )
    db.commit()
    return cur.lastrowid


def upsert_edge(
    db: DatabaseConnection,
    scope: str,
    source_node_id: int,
    target_node_id: int,
    relation: str,
    description: str,
    user_guid: str,
) -> int:
    """Create or merge an edge keyed by (scope, source, target, relation). Returns id."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT id FROM kg_edges
            WHERE scope = {ph} AND source_node_id = {ph} AND target_node_id = {ph}
              AND LOWER(relation) = {ph} AND user_guid = {ph}""",
        (scope, source_node_id, target_node_id, relation.strip().lower(), user_guid),
    )
    row = cur.fetchone()
    if row:
        edge_id = row[0]
        db.execute(f"UPDATE kg_edges SET weight = weight + 1 WHERE id = {ph}", (edge_id,))
        db.commit()
        return edge_id
    cur = db.execute(
        f"""INSERT INTO kg_edges (scope, source_node_id, target_node_id, relation,
                                  description, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (scope, source_node_id, target_node_id, relation.strip(), description, user_guid),
    )
    db.commit()
    return cur.lastrowid


def get_nodes(
    db: DatabaseConnection, scope: str, user_guid: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Nodes in a scope, heaviest first."""
    ph = db.placeholder
    sql = (
        f"SELECT {', '.join(_NODE_COLUMNS)} FROM kg_nodes "
        f"WHERE scope = {ph} AND user_guid = {ph} ORDER BY weight DESC, id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = db.execute(sql, (scope, user_guid))
    return [dict(zip(_NODE_COLUMNS, row)) for row in cur.fetchall()]


def get_edges(db: DatabaseConnection, scope: str, user_guid: str) -> list[dict[str, Any]]:
    """All edges in a scope."""
    ph = db.placeholder
    cur = db.execute(
        f"SELECT {', '.join(_EDGE_COLUMNS)} FROM kg_edges "
        f"WHERE scope = {ph} AND user_guid = {ph} ORDER BY weight DESC, id",
        (scope, user_guid),
    )
    return [dict(zip(_EDGE_COLUMNS, row)) for row in cur.fetchall()]


def get_edges_for_nodes(
    db: DatabaseConnection, scope: str, node_ids: list[int], user_guid: str
) -> list[dict[str, Any]]:
    """Edges in a scope touching any of the given nodes."""
    if not node_ids:
        return []
    ph = db.placeholder
    marks = ", ".join([ph] * len(node_ids))
    cur = db.execute(
        f"SELECT {', '.join(_EDGE_COLUMNS)} FROM kg_edges "
        f"WHERE scope = {ph} AND user_guid = {ph} "
        f"AND (source_node_id IN ({marks}) OR target_node_id IN ({marks})) "
        f"ORDER BY weight DESC, id",
        (scope, user_guid, *node_ids, *node_ids),
    )
    return [dict(zip(_EDGE_COLUMNS, row)) for row in cur.fetchall()]


def get_nodes_by_ids(
    db: DatabaseConnection, scope: str, node_ids: list[int], user_guid: str
) -> list[dict[str, Any]]:
    """Specific nodes in a scope by id."""
    if not node_ids:
        return []
    ph = db.placeholder
    marks = ", ".join([ph] * len(node_ids))
    cur = db.execute(
        f"SELECT {', '.join(_NODE_COLUMNS)} FROM kg_nodes "
        f"WHERE scope = {ph} AND user_guid = {ph} AND id IN ({marks})",
        (scope, user_guid, *node_ids),
    )
    return [dict(zip(_NODE_COLUMNS, row)) for row in cur.fetchall()]


def count_nodes(db: DatabaseConnection, scope: str, user_guid: str) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"SELECT COUNT(*) FROM kg_nodes WHERE scope = {ph} AND user_guid = {ph}",
        (scope, user_guid),
    )
    return int(cur.fetchone()[0])


def count_edges(db: DatabaseConnection, scope: str, user_guid: str) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"SELECT COUNT(*) FROM kg_edges WHERE scope = {ph} AND user_guid = {ph}",
        (scope, user_guid),
    )
    return int(cur.fetchone()[0])


def clear_scope(db: DatabaseConnection, scope: str, user_guid: str) -> None:
    """Remove a scope's nodes, edges, and build state."""
    ph = db.placeholder
    for table in ("kg_edges", "kg_nodes", "kg_builds"):
        db.execute(
            f"DELETE FROM {table} WHERE scope = {ph} AND user_guid = {ph}",
            (scope, user_guid),
        )
    db.commit()


def get_build_state(db: DatabaseConnection, scope: str, user_guid: str) -> list[dict[str, Any]]:
    """Watermark rows for a scope."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT id, scope, source_key, watermark, built_at, status, error
            FROM kg_builds WHERE scope = {ph} AND user_guid = {ph} ORDER BY source_key""",
        (scope, user_guid),
    )
    cols = ("id", "scope", "source_key", "watermark", "built_at", "status", "error")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def set_watermark(
    db: DatabaseConnection,
    scope: str,
    source_key: str,
    watermark: int,
    user_guid: str,
    *,
    status: str = "complete",
    error: str | None = None,
) -> None:
    """Upsert the watermark row for (scope, source_key)."""
    ph = db.placeholder
    cur = db.execute(
        f"""UPDATE kg_builds
            SET watermark = {ph}, built_at = CURRENT_TIMESTAMP, status = {ph}, error = {ph}
            WHERE scope = {ph} AND source_key = {ph} AND user_guid = {ph}""",
        (watermark, status, error, scope, source_key, user_guid),
    )
    if cur.rowcount == 0:
        db.execute(
            f"""INSERT INTO kg_builds (scope, source_key, watermark, built_at, status,
                                       error, user_guid)
                VALUES ({ph}, {ph}, {ph}, CURRENT_TIMESTAMP, {ph}, {ph}, {ph})""",
            (scope, source_key, watermark, status, error, user_guid),
        )
    db.commit()
