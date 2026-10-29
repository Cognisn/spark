"""Unique natural keys on knowledge graph nodes and edges, skills and conversation skills.

Revision ID: vector_store_2
Revises: vector_store_1

Concurrent first inserts could duplicate these rows (a select-then-insert race
on PostgreSQL). The upgrade, on both engines:

1. adds ``kg_edges.relation_key`` (the relation stripped and lower-cased in
   Python, as the repository has always matched it);
2. merges duplicates, keeping the earliest row (lowest id):
   * kg_nodes by (user_guid, scope, name_key): weights summed, the most recent
     non-empty description and non-null embedding, the latest ``updated_at``;
     edges pointing at a merged node are re-pointed to the kept one;
   * kg_edges by (user_guid, scope, source, target, relation_key), after the
     re-pointing: weights summed;
   * skills by (user_guid, name) and conversation_skills by
     (conversation_id, skill_name): the most recent ``enabled`` state;
3. adds a unique index on each natural key.

The downgrade drops the unique indexes and ``relation_key``, restoring the
unconstrained schema; merged rows stay merged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "vector_store_2"
down_revision = "vector_store_1"
branch_labels = None
depends_on = None

_INDEXES = (
    ("uq_kg_nodes_natural_key", "kg_nodes", "user_guid, scope, name_key"),
    (
        "uq_kg_edges_natural_key",
        "kg_edges",
        "user_guid, scope, source_node_id, target_node_id, relation_key",
    ),
    ("uq_skills_natural_key", "skills", "user_guid, name"),
    ("uq_conversation_skills_natural_key", "conversation_skills", "conversation_id, skill_name"),
)


def _rows(sql: str, params: dict[str, Any] | None = None) -> Sequence[sa.Row[Any]]:
    return op.get_bind().execute(sa.text(sql), params or {}).fetchall()


def _run(sql: str, params: dict[str, Any] | list[dict[str, Any]] | None = None) -> None:
    op.get_bind().execute(sa.text(sql), params or {})


def _duplicate_groups(table: str, key: str) -> list[list[sa.Row[Any]]]:
    """Every group of rows sharing ``key``, each group ordered by id (earliest first)."""
    groups = _rows(f"SELECT {key} FROM {table} GROUP BY {key} HAVING COUNT(*) > 1")
    columns = [c.strip() for c in key.split(",")]
    match = " AND ".join(f"{c} = :{c}" for c in columns)
    return [
        list(_rows(f"SELECT * FROM {table} WHERE {match} ORDER BY id", dict(zip(columns, g))))
        for g in groups
    ]


def _relation_keys() -> None:
    op.execute("ALTER TABLE kg_edges ADD COLUMN relation_key TEXT NOT NULL DEFAULT ''")
    rows = _rows("SELECT id, relation FROM kg_edges")
    if rows:
        _run(
            "UPDATE kg_edges SET relation_key = :key WHERE id = :id",
            [{"id": r.id, "key": (r.relation or "").strip().lower()} for r in rows],
        )


def _merge_nodes() -> None:
    for group in _duplicate_groups("kg_nodes", "user_guid, scope, name_key"):
        keep, merged = group[0], group[1:]
        latest = list(reversed(group))
        description = next((r.description for r in latest if r.description), keep.description)
        embedding = next((r.embedding for r in latest if r.embedding is not None), None)
        updated = max((r.updated_at for r in group if r.updated_at is not None), default=None)
        _run(
            "UPDATE kg_nodes SET weight = :weight, description = :description, "
            "embedding = :embedding, updated_at = :updated WHERE id = :id",
            {
                "id": keep.id,
                "weight": sum(r.weight or 0 for r in group),
                "description": description,
                "embedding": embedding,
                "updated": updated,
            },
        )
        for row in merged:
            params = {"keep": keep.id, "dup": row.id}
            _run("UPDATE kg_edges SET source_node_id = :keep WHERE source_node_id = :dup", params)
            _run("UPDATE kg_edges SET target_node_id = :keep WHERE target_node_id = :dup", params)
            _run("DELETE FROM kg_nodes WHERE id = :dup", params)


def _merge_edges() -> None:
    key = "user_guid, scope, source_node_id, target_node_id, relation_key"
    for group in _duplicate_groups("kg_edges", key):
        keep = group[0]
        _run(
            "UPDATE kg_edges SET weight = :weight WHERE id = :id",
            {"id": keep.id, "weight": sum(r.weight or 0 for r in group)},
        )
        for row in group[1:]:
            _run("DELETE FROM kg_edges WHERE id = :id", {"id": row.id})


def _merge_states(table: str, key: str) -> None:
    for group in _duplicate_groups(table, key):
        keep, newest = group[0], group[-1]
        _run(
            f"UPDATE {table} SET enabled = :enabled WHERE id = :id",
            {"id": keep.id, "enabled": newest.enabled},
        )
        if table == "skills":
            updated = max((r.updated_at for r in group if r.updated_at is not None), default=None)
            _run(
                "UPDATE skills SET updated_at = :updated WHERE id = :id",
                {"id": keep.id, "updated": updated},
            )
        for row in group[1:]:
            _run(f"DELETE FROM {table} WHERE id = :id", {"id": row.id})


def upgrade() -> None:
    _relation_keys()
    _merge_nodes()
    _merge_edges()
    _merge_states("skills", "user_guid, name")
    _merge_states("conversation_skills", "conversation_id, skill_name")
    for name, table, columns in _INDEXES:
        op.execute(f"CREATE UNIQUE INDEX {name} ON {table} ({columns})")


def downgrade() -> None:
    for name, _, _ in reversed(_INDEXES):
        op.execute(f"DROP INDEX {name}")
    op.execute("ALTER TABLE kg_edges DROP COLUMN relation_key")
