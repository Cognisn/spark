"""Persistent user memory with semantic search.

Vectors come from an :class:`~spark.index.vectors.EmbeddingModel` and live in the
mode's :class:`~spark.index.vectors.VectorStore` (SQLite columns in personal mode,
pgvector in server mode). The index runs on worker threads and reaches the
database through the bridge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from spark.index.embeddings import SentenceTransformerEmbedding
from spark.index.vectors import MEMORIES, EmbeddingModel, Vector, VectorStore, vector_store

if TYPE_CHECKING:
    from spark.db.bridge import SyncDatabase
    from spark.db.engine import Executor

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"preferences", "facts", "projects", "instructions", "relationships"}

_REBUILD_BATCH = 64


async def _add_memory(
    db: Executor,
    /,
    store: VectorStore,
    user_guid: str,
    content: str,
    category: str,
    embedding: Vector,
    **kwargs: Any,
) -> int | None:
    """Insert the memory and record its vector in one transaction (a refused vector undoes both)."""
    from spark.database import memories

    async with db.transaction() as conn:
        memory_id = await memories.add_memory(
            conn, user_guid, content, category, embedding, **kwargs
        )
        if memory_id is not None:
            await store.add(conn, user_guid, memory_id, embedding)
    return memory_id


async def _update_memory(
    db: Executor,
    /,
    store: VectorStore,
    memory_id: int,
    user_guid: str,
    embedding: Vector | None,
    **kwargs: Any,
) -> None:
    from spark.database import memories

    async with db.transaction() as conn:
        await memories.update_memory(conn, memory_id, user_guid, embedding=embedding, **kwargs)
        if embedding is not None:
            await store.add(conn, user_guid, memory_id, embedding)


async def _delete_memory(
    db: Executor, /, store: VectorStore, memory_id: int, user_guid: str
) -> None:
    from spark.database import memories

    async with db.transaction() as conn:
        await store.delete(conn, user_guid, [memory_id])
        await memories.delete_memory(conn, memory_id, user_guid)


class MemoryIndex:
    """Manages persistent user memories with vector-based semantic search."""

    def __init__(
        self,
        db: SyncDatabase,
        user_guid: str,
        config: dict[str, Any] | None = None,
        *,
        embedder: EmbeddingModel | None = None,
        store: VectorStore | None = None,
    ) -> None:
        self._db = db
        self._user_guid = user_guid
        config = config or {}
        emb_config = config.get("embedding", {})
        self._embedder: EmbeddingModel = embedder or SentenceTransformerEmbedding(
            model_name=emb_config.get("model", "all-MiniLM-L6-v2"),
            device=emb_config.get("device", "cpu"),
            batch_size=emb_config.get("batch_size", 32),
        )
        self._store = store
        auto = config.get("auto_retrieval", {})
        self._auto_enabled = auto.get("enabled", True)
        self._auto_top_k = auto.get("top_k", 5)
        self._auto_threshold = auto.get("threshold", 0.4)

    @property
    def vector_store(self) -> VectorStore:
        """The mode's store, chosen from the database's dialect on first use."""
        if self._store is None:
            self._store = vector_store(MEMORIES, self._db.database.dialect)
        return self._store

    def _embed(self, text: str) -> NDArray[np.float32]:
        row: NDArray[np.float32] = self._embedder.embed([text])[0]
        return row

    def store(
        self,
        content: str,
        category: str = "facts",
        *,
        importance: float = 0.5,
        source_conversation_id: int | None = None,
        metadata: str | None = None,
    ) -> int | None:
        """Store a memory. Returns ID or None if duplicate."""
        from spark.database import memories

        if category not in VALID_CATEGORIES:
            category = "facts"

        if self._db.call(memories.memory_exists, self._user_guid, content):
            return None

        embedding = self._embed(content)
        return self._db.call(
            _add_memory,
            self.vector_store,
            self._user_guid,
            content,
            category,
            embedding,
            importance=importance,
            source_conversation_id=source_conversation_id,
            metadata_json=metadata,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        categories: list[str] | None = None,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Search memories by semantic similarity."""
        from spark.database import memories

        query_embedding = self._embed(query)
        store = self.vector_store

        if categories:
            results: list[dict[str, Any]] = []
            for cat in categories:
                if cat in VALID_CATEGORIES:
                    results.extend(
                        self._db.call(
                            store.search,
                            self._user_guid,
                            query_embedding,
                            top_k=top_k,
                            threshold=threshold,
                            where={"category": cat},
                        )
                    )
            results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
            results = results[:top_k]
        else:
            results = self._db.call(
                store.search,
                self._user_guid,
                query_embedding,
                top_k=top_k,
                threshold=threshold,
            )

        # Update access timestamps
        if results:
            ids = [r["id"] for r in results if "id" in r]
            self._db.call(memories.update_last_accessed, ids)

        return results

    def auto_retrieve(
        self,
        user_message: str,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> str | None:
        """Auto-retrieve relevant memories for a user message.

        Returns a formatted context block, or None if no matches.
        """
        if not self._auto_enabled:
            return None

        results = self.search(
            user_message,
            top_k=top_k or self._auto_top_k,
            threshold=threshold or self._auto_threshold,
        )

        if not results:
            return None

        lines = ["## Relevant Memories\n"]
        for mem in results:
            cat = mem.get("category", "facts")
            content = mem.get("content", "")
            lines.append(f"- [{cat}] {content}")

        return "\n".join(lines)

    def update(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        importance: float | None = None,
    ) -> bool:
        """Update an existing memory."""
        from spark.database import memories

        mem = self._db.call(memories.get_memory, memory_id, self._user_guid)
        if mem is None:
            return False

        # Re-embed if content changed
        new_content = content if content and content != mem.get("content") else None
        self._db.call(
            _update_memory,
            self.vector_store,
            memory_id,
            self._user_guid,
            self._embed(new_content) if new_content else None,
            content=new_content,
            category=category,
            importance=importance,
        )
        return True

    def delete(self, memory_id: int) -> bool:
        """Delete a memory."""
        from spark.database import memories

        mem = self._db.call(memories.get_memory, memory_id, self._user_guid)
        if mem is None:
            return False
        self._db.call(_delete_memory, self.vector_store, memory_id, self._user_guid)
        return True

    def list_all(self, *, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List all memories, optionally filtered by category."""
        from spark.database import memories

        all_mems = self._db.call(memories.get_all_memories, self._user_guid, limit=limit)
        if category:
            return [m for m in all_mems if m.get("category") == category]
        return all_mems

    def get(self, memory_id: int) -> dict[str, Any] | None:
        """Get a single memory."""
        from spark.database import memories

        return self._db.call(memories.get_memory, memory_id, self._user_guid)

    def clear_all(self) -> None:
        """Delete all memories for this user."""
        from spark.database import memories

        self._db.call(memories.delete_all_memories, self._user_guid)

    def rebuild(self) -> int:
        """Re-embed every memory of this user with the current model; returns how many.

        Needed after changing the embedding model: until then a model of another
        dimension is refused with EmbeddingDimensionMismatch.
        """
        store = self.vector_store
        items = self._db.call(store.items, self._user_guid)
        vectors: list[tuple[int, Vector]] = []
        for start in range(0, len(items), _REBUILD_BATCH):
            batch = items[start : start + _REBUILD_BATCH]
            embedded = self._embedder.embed([text for _, text in batch])
            vectors.extend((item_id, embedded[i]) for i, (item_id, _) in enumerate(batch))
        return self._db.call(store.rebuild, self._user_guid, vectors)
