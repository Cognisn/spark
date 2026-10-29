"""pgvector tables for memories and conversation context (server mode only).

Revision ID: vector_store_1
Revises: 0001

On PostgreSQL: the ``vector`` extension (created in ``public`` if it is not
there yet; the database role needs the right to create it, or an administrator
creates it first), one vector table per collection keyed by the item with
``ON DELETE CASCADE``, each vector's dimension recorded and checked, and a
partial HNSW cosine index for 384 dimensions (the default local model,
all-MiniLM-L6-v2). The store adds indexes for other dimensions when it
rebuilds. Embeddings already stored in the items' ``embedding`` columns are
copied in. On SQLite nothing changes: personal mode keeps those columns.

The downgrade drops the tables and leaves the extension, which may predate
this revision or serve other schemas.
"""

from __future__ import annotations

import numpy as np
import sqlalchemy as sa
from alembic import op

revision = "vector_store_1"
down_revision = "0001"
branch_labels = None
depends_on = None

# (vector table, key column, item table)
_TABLES = (
    ("memory_vectors", "memory_id", "user_memories"),
    ("context_vectors", "element_id", "context_index_elements"),
)
_DEFAULT_DIMENSION = 384


def _vector_text(blob: bytes) -> str | None:
    if not blob or len(blob) % 4:
        return None
    values = np.frombuffer(blob, dtype=np.float32)
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    for table, key, items in _TABLES:
        op.execute(
            f"CREATE TABLE {table} ("
            f"{key} INTEGER PRIMARY KEY REFERENCES {items}(id) ON DELETE CASCADE, "
            "dimension INTEGER NOT NULL CHECK (dimension > 0), "
            "embedding vector NOT NULL, "
            f"CONSTRAINT {table}_dimension CHECK (vector_dims(embedding) = dimension))"
        )
        d = _DEFAULT_DIMENSION
        op.execute(
            f"CREATE INDEX {table}_hnsw_{d} ON {table} "
            f"USING hnsw ((embedding::vector({d})) vector_cosine_ops) WHERE dimension = {d}"
        )
        rows = bind.execute(
            sa.text(f"SELECT id, embedding FROM {items} WHERE embedding IS NOT NULL ORDER BY id")
        ).fetchall()
        copies = []
        for item_id, blob in rows:
            text = _vector_text(bytes(blob))
            if text is not None:
                copies.append({"id": item_id, "d": len(blob) // 4, "v": text})
        if copies:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} ({key}, dimension, embedding) "
                    "VALUES (:id, :d, CAST(:v AS vector))"
                ),
                copies,
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, _, _ in reversed(_TABLES):
        op.execute(f"DROP TABLE {table}")
