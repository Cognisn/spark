"""Embeddings and their storage behind two interfaces, one implementation per mode.

* :class:`EmbeddingModel` turns texts into vectors. The local implementation is
  :class:`spark.index.embeddings.SentenceTransformerEmbedding` (CPU); provider
  embeddings can implement the same protocol later.
* :class:`VectorStore` keeps one vector per item of a :class:`Collection`
  (memories, or conversation context elements) and searches them for one user.

Personal mode (SQLite) keeps the 0.2.0 approach: the vector is the item's own
``embedding`` column (float32 bytes) and search computes cosine similarity in
Python over the candidate rows. Server mode (PostgreSQL) also keeps that column
and adds a pgvector table per collection (``memory_vectors``, ``context_vectors``,
revision ``vector_store_1``) with a partial HNSW cosine index per dimension;
search runs in the database with the user filter in the same query.

Each stored vector's dimension is recorded (the byte length on SQLite, a
``dimension`` column on PostgreSQL). A vector or query whose dimension differs
from what a user already has stored is refused with
:class:`EmbeddingDimensionMismatch`: changing the embedding model needs
:meth:`VectorStore.rebuild`, which replaces all of the user's vectors at once.

Store methods are async repository functions over :class:`spark.db.Executor`;
synchronous index code calls them through :class:`spark.db.SyncDatabase`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from spark.db.engine import Executor, Row
from spark.mode import Dialect

__all__ = [
    "CONTEXT",
    "MEMORIES",
    "Collection",
    "EmbeddingDimensionMismatch",
    "EmbeddingModel",
    "Filters",
    "PgVectorStore",
    "SqliteVectorStore",
    "Vector",
    "VectorStore",
    "hnsw_index_name",
    "vector_store",
]

Vector = NDArray[Any]
Filters = Mapping[str, Any]

_FLOAT32_BYTES = 4


@runtime_checkable
class EmbeddingModel(Protocol):
    """Turns texts into fixed-width vectors."""

    @property
    def name(self) -> str:
        """The model's name, for messages and logs."""
        ...

    @property
    def dimension(self) -> int:
        """The width of every vector this model produces."""
        ...

    def embed(self, texts: Sequence[str]) -> NDArray[np.float32]:
        """L2-normalised float32 vectors, shape ``(len(texts), dimension)``.

        CPU-bound and synchronous: call it on a worker thread, never on the event loop.
        """
        ...


class EmbeddingDimensionMismatch(ValueError):
    """A vector's dimension differs from the dimension of the user's stored vectors."""

    def __init__(self, collection: str, stored: int, given: int) -> None:
        self.collection = collection
        self.stored = stored
        self.given = given
        super().__init__(
            f"the {collection} index holds {stored}-dimension vectors but this vector has "
            f"{given} dimensions (the embedding model changed?); rebuild the {collection} "
            "index with the current model before adding or searching"
        )


@dataclass(frozen=True)
class Collection:
    """Items that carry a vector: their table, text and the filters search accepts."""

    name: str
    table: str
    text_column: str
    vector_table: str
    key_column: str
    filters: frozenset[str]
    required: frozenset[str] = field(default_factory=frozenset)


MEMORIES = Collection(
    name="memories",
    table="user_memories",
    text_column="content",
    vector_table="memory_vectors",
    key_column="memory_id",
    filters=frozenset({"category"}),
)
CONTEXT = Collection(
    name="context",
    table="context_index_elements",
    text_column="content_text",
    vector_table="context_vectors",
    key_column="element_id",
    filters=frozenset({"conversation_id", "element_type"}),
    required=frozenset({"conversation_id"}),
)


@runtime_checkable
class VectorStore(Protocol):
    """One vector per item of a collection, searched per user."""

    @property
    def collection(self) -> Collection: ...

    async def add(self, db: Executor, user_guid: str, item_id: int, vector: Vector) -> None:
        """Record (or replace) the vector of the user's item.

        Raises :class:`LookupError` when the item is not the user's, and
        :class:`EmbeddingDimensionMismatch` when the user's other vectors differ in width.
        """
        ...

    async def delete(self, db: Executor, user_guid: str, item_ids: Sequence[int]) -> int:
        """Remove the vectors of the user's items; returns how many items matched."""
        ...

    async def search(
        self,
        db: Executor,
        user_guid: str,
        query: Vector,
        *,
        top_k: int = 10,
        threshold: float = 0.3,
        where: Filters | None = None,
    ) -> list[Row]:
        """The user's items most similar to ``query`` (cosine), best first.

        Each row is the item's row plus ``similarity``; at most ``top_k`` rows, none
        below ``threshold``. ``where`` narrows by the collection's filter columns
        (a list value means any of).
        """
        ...

    async def items(self, db: Executor, user_guid: str) -> list[tuple[int, str]]:
        """Every item of the user as ``(id, text)``, for re-embedding."""
        ...

    async def rebuild(
        self, db: Executor, user_guid: str, vectors: Sequence[tuple[int, Vector]]
    ) -> int:
        """Replace all of the user's vectors with ``vectors`` in one transaction.

        Items of the user left out have no vector afterwards; an item that is not
        the user's raises :class:`LookupError` and nothing changes.
        """
        ...


def hnsw_index_name(collection: Collection, dimension: int) -> str:
    """The partial HNSW index serving one dimension of a collection's vectors."""
    return f"{collection.vector_table}_hnsw_{int(dimension)}"


def _as_vector(vector: Vector) -> NDArray[np.float32]:
    return np.asarray(vector, dtype=np.float32).reshape(-1)


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


class _ColumnStore:
    """Vectors in the item's own ``embedding`` column (float32 bytes): both engines keep it."""

    def __init__(self, collection: Collection) -> None:
        self._collection = collection

    @property
    def collection(self) -> Collection:
        return self._collection

    def check_filters(self, where: Filters | None) -> dict[str, Any]:
        where = dict(where or {})
        unknown = set(where) - self._collection.filters
        if unknown:
            raise ValueError(
                f"{self._collection.name} search cannot filter on {', '.join(sorted(unknown))}"
            )
        missing = self._collection.required - set(where)
        if missing:
            raise ValueError(f"{self._collection.name} search needs {', '.join(sorted(missing))}")
        return where

    async def _stored_dimension_other_than(
        self, db: Executor, user_guid: str, dimension: int, *, exclude: int | None = None
    ) -> int | None:
        c = self._collection
        width = await db.fetch_value(
            f"SELECT length(embedding) FROM {c.table} "
            "WHERE user_guid = ? AND embedding IS NOT NULL AND length(embedding) <> ? "
            "AND id <> ? LIMIT 1",
            (user_guid, dimension * _FLOAT32_BYTES, -1 if exclude is None else exclude),
        )
        return None if width is None else int(width) // _FLOAT32_BYTES

    async def _check_dimension(
        self, db: Executor, user_guid: str, dimension: int, *, exclude: int | None = None
    ) -> None:
        stored = await self._stored_dimension_other_than(db, user_guid, dimension, exclude=exclude)
        if stored is not None:
            raise EmbeddingDimensionMismatch(self._collection.name, stored, dimension)

    async def _write_column(
        self, db: Executor, user_guid: str, item_id: int, vector: NDArray[np.float32]
    ) -> None:
        changed = await db.execute(
            f"UPDATE {self._collection.table} SET embedding = ? WHERE id = ? AND user_guid = ?",
            (vector.tobytes(), item_id, user_guid),
        )
        if changed == 0:
            raise LookupError(f"{self._collection.name} item {item_id} is not this user's")

    async def _clear_column(self, db: Executor, user_guid: str, item_ids: Sequence[int]) -> int:
        if not item_ids:
            return 0
        return await db.execute(
            f"UPDATE {self._collection.table} SET embedding = NULL "
            f"WHERE user_guid = ? AND id IN ({_marks(item_ids)})",
            (user_guid, *item_ids),
        )

    async def items(self, db: Executor, user_guid: str) -> list[tuple[int, str]]:
        c = self._collection
        rows = await db.fetch_all(
            f"SELECT id, {c.text_column} AS text FROM {c.table} WHERE user_guid = ? ORDER BY id",
            (user_guid,),
        )
        return [(int(r["id"]), str(r["text"])) for r in rows]


class SqliteVectorStore(_ColumnStore):
    """Personal mode: the 0.2.0 approach, cosine similarity in Python over the item rows."""

    async def add(self, db: Executor, user_guid: str, item_id: int, vector: Vector) -> None:
        vec = _as_vector(vector)
        async with db.transaction() as conn:
            await self._check_dimension(conn, user_guid, vec.shape[0], exclude=item_id)
            await self._write_column(conn, user_guid, item_id, vec)

    async def delete(self, db: Executor, user_guid: str, item_ids: Sequence[int]) -> int:
        return await self._clear_column(db, user_guid, list(item_ids))

    async def search(
        self,
        db: Executor,
        user_guid: str,
        query: Vector,
        *,
        top_k: int = 10,
        threshold: float = 0.3,
        where: Filters | None = None,
    ) -> list[Row]:
        from spark.database import context_index, memories

        filters = self.check_filters(where)
        vec = _as_vector(query)
        await self._check_dimension(db, user_guid, vec.shape[0])
        if self._collection is MEMORIES:
            return await memories.search_memories(
                db,
                user_guid,
                vec,
                top_k=top_k,
                threshold=threshold,
                category=filters.get("category"),
            )
        # Context search stays scoped by conversation as in 0.2.0: personal mode has one
        # user, and rows from older versions may carry another user_guid for that user.
        ids = filters["conversation_id"]
        return await context_index.search_context(
            db,
            list(ids) if isinstance(ids, (list, tuple)) else [ids],
            vec,
            top_k=top_k,
            threshold=threshold,
            element_type=filters.get("element_type"),
        )

    async def rebuild(
        self, db: Executor, user_guid: str, vectors: Sequence[tuple[int, Vector]]
    ) -> int:
        c = self._collection
        async with db.transaction() as conn:
            await conn.execute(
                f"UPDATE {c.table} SET embedding = NULL WHERE user_guid = ?", (user_guid,)
            )
            for item_id, vector in vectors:
                await self._write_column(conn, user_guid, item_id, _as_vector(vector))
        return len(vectors)


class PgVectorStore(_ColumnStore):
    """Server mode: pgvector, HNSW cosine search in the database filtered to the user."""

    def _hnsw_sql(self, dimension: int) -> str:
        c = self._collection
        d = int(dimension)
        return (
            f"CREATE INDEX IF NOT EXISTS {hnsw_index_name(c, d)} ON {c.vector_table} "
            f"USING hnsw ((embedding::vector({d})) vector_cosine_ops) WHERE dimension = {d}"
        )

    async def _stored_dimension_other_than(
        self, db: Executor, user_guid: str, dimension: int, *, exclude: int | None = None
    ) -> int | None:
        c = self._collection
        stored = await db.fetch_value(
            f"SELECT v.dimension FROM {c.vector_table} v JOIN {c.table} t ON t.id = v.{c.key_column} "
            f"WHERE t.user_guid = ? AND v.dimension <> ? AND v.{c.key_column} <> ? LIMIT 1",
            (user_guid, dimension, -1 if exclude is None else exclude),
        )
        return None if stored is None else int(stored)

    async def _write_vector(
        self, db: Executor, user_guid: str, item_id: int, vector: NDArray[np.float32]
    ) -> None:
        c = self._collection
        changed = await db.execute(
            f"INSERT INTO {c.vector_table} ({c.key_column}, dimension, embedding) "
            f"SELECT id, ?, ? FROM {c.table} WHERE id = ? AND user_guid = ? "
            f"ON CONFLICT ({c.key_column}) DO UPDATE "
            "SET dimension = excluded.dimension, embedding = excluded.embedding",
            (vector.shape[0], vector, item_id, user_guid),
        )
        if changed == 0:
            raise LookupError(f"{c.name} item {item_id} is not this user's")

    async def add(self, db: Executor, user_guid: str, item_id: int, vector: Vector) -> None:
        vec = _as_vector(vector)
        async with db.transaction() as conn:
            await self._check_dimension(conn, user_guid, vec.shape[0], exclude=item_id)
            await self._write_column(conn, user_guid, item_id, vec)
            await self._write_vector(conn, user_guid, item_id, vec)

    async def delete(self, db: Executor, user_guid: str, item_ids: Sequence[int]) -> int:
        c = self._collection
        ids = list(item_ids)
        if not ids:
            return 0
        async with db.transaction() as conn:
            await conn.execute(
                f"DELETE FROM {c.vector_table} WHERE {c.key_column} IN "
                f"(SELECT id FROM {c.table} WHERE user_guid = ? AND id IN ({_marks(ids)}))",
                (user_guid, *ids),
            )
            return await self._clear_column(conn, user_guid, ids)

    def _search_sql(self, filters: dict[str, Any], dimension: int) -> tuple[str, list[Any]] | None:
        c = self._collection
        d = int(dimension)
        distance = f"v.embedding::vector({d}) <=> ?::vector({d})"
        clauses = ["t.user_guid = ?", f"v.dimension = {d}"]
        params: list[Any] = []
        for column in sorted(filters):
            value = filters[column]
            if isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
                if not values:
                    return None
                clauses.append(f"t.{column} IN ({_marks(values)})")
                params.extend(values)
            elif value is not None:
                clauses.append(f"t.{column} = ?")
                params.append(value)
        sql = (
            f"SELECT t.*, 1 - ({distance}) AS similarity "
            f"FROM {c.vector_table} v JOIN {c.table} t ON t.id = v.{c.key_column} "
            f"WHERE {' AND '.join(clauses)} ORDER BY {distance} LIMIT ?"
        )
        return sql, params

    async def search(
        self,
        db: Executor,
        user_guid: str,
        query: Vector,
        *,
        top_k: int = 10,
        threshold: float = 0.3,
        where: Filters | None = None,
    ) -> list[Row]:
        filters = self.check_filters(where)
        vec = _as_vector(query)
        built = self._search_sql(filters, vec.shape[0])
        if built is None or not np.any(vec):
            return []
        sql, params = built
        async with db.transaction() as conn:
            await self._check_dimension(conn, user_guid, vec.shape[0])
            # Keep scanning the HNSW graph until enough rows pass the filters (pgvector 0.8).
            await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            rows = await conn.fetch_all(sql, (vec, user_guid, *params, vec, top_k))
        results = []
        for row in rows:
            row["similarity"] = float(row["similarity"])
            if row["similarity"] < threshold:
                continue
            if self._collection is MEMORIES and row.get("embedding"):
                row["embedding"] = np.frombuffer(row["embedding"], dtype=np.float32)
            else:
                row.pop("embedding", None)
            results.append(row)
        return results

    async def explain(
        self,
        db: Executor,
        user_guid: str,
        query: Vector,
        *,
        top_k: int = 10,
        where: Filters | None = None,
    ) -> str:
        """The query plan of :meth:`search` (diagnostics: is the HNSW index used?)."""
        vec = _as_vector(query)
        built = self._search_sql(self.check_filters(where), vec.shape[0])
        if built is None:
            return ""
        sql, params = built
        async with db.transaction() as conn:
            await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            rows = await conn.fetch_all("EXPLAIN " + sql, (vec, user_guid, *params, vec, top_k))
        return "\n".join(str(next(iter(r.values()))) for r in rows)

    async def rebuild(
        self, db: Executor, user_guid: str, vectors: Sequence[tuple[int, Vector]]
    ) -> int:
        c = self._collection
        prepared = [(item_id, _as_vector(vector)) for item_id, vector in vectors]
        async with db.transaction() as conn:
            await conn.execute(
                f"DELETE FROM {c.vector_table} WHERE {c.key_column} IN "
                f"(SELECT id FROM {c.table} WHERE user_guid = ?)",
                (user_guid,),
            )
            await conn.execute(
                f"UPDATE {c.table} SET embedding = NULL WHERE user_guid = ?", (user_guid,)
            )
            for item_id, vec in prepared:
                await self._write_column(conn, user_guid, item_id, vec)
                await self._write_vector(conn, user_guid, item_id, vec)
            for dimension in sorted({vec.shape[0] for _, vec in prepared}):
                await conn.execute(self._hnsw_sql(dimension))
        return len(prepared)


def vector_store(collection: Collection, dialect: Dialect) -> VectorStore:
    """The store for a collection on this engine."""
    if dialect is Dialect.POSTGRESQL:
        return PgVectorStore(collection)
    if dialect is Dialect.SQLITE:
        return SqliteVectorStore(collection)
    raise ValueError(f"no vector store for dialect {dialect!r}")
