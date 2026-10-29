"""MemoryIndex and ConversationVectorIndex over the VectorStore and EmbeddingModel interfaces.

The indexes are synchronous (worker threads) and reach the store through the
bridge; ``sync_db`` is personal mode (SQLite). A small deterministic embedding
model stands in for sentence-transformers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from spark.database import conversations
from spark.index.memory_index import MemoryIndex
from spark.index.vector_index import ConversationVectorIndex
from spark.index.vectors import (
    CONTEXT,
    MEMORIES,
    EmbeddingDimensionMismatch,
    EmbeddingModel,
    SqliteVectorStore,
)

USER = "user-carol-example"


class HashEmbedding:
    """Deterministic unit vectors from a text hash; the same text always gives the same vector."""

    def __init__(self, dimension: int = 16, name: str = "hash-model") -> None:
        self._dimension = dimension
        self._name = name
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> NDArray[np.float32]:
        self.calls.append(list(texts))
        rows = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
            vec = np.random.default_rng(seed).normal(size=self._dimension).astype(np.float32)
            rows.append(vec / np.linalg.norm(vec))
        return np.array(rows, dtype=np.float32).reshape(len(texts), self._dimension)


def test_the_stand_in_is_an_embedding_model() -> None:
    assert isinstance(HashEmbedding(), EmbeddingModel)


@pytest.fixture()
def memory_index(sync_db: Any) -> MemoryIndex:
    return MemoryIndex(sync_db, USER, embedder=HashEmbedding())


@pytest.fixture()
def conversation(sync_db: Any) -> int:
    return int(sync_db.call(conversations.create_conversation, "Index", "model", USER))


class TestMemoryIndexOverTheStore:
    def test_personal_mode_uses_the_sqlite_store(self, memory_index: MemoryIndex) -> None:
        assert isinstance(memory_index.vector_store, SqliteVectorStore)
        assert memory_index.vector_store.collection is MEMORIES

    def test_store_then_search_finds_the_memory(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("the office is in Example Street", "facts")

        hits = memory_index.search("the office is in Example Street", threshold=0.9)

        assert [h["id"] for h in hits] == [mid]
        assert hits[0]["similarity"] == pytest.approx(1.0, abs=1e-5)

    def test_updated_content_is_searchable_by_its_new_text(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("old wording", "facts")
        assert mid is not None

        assert memory_index.update(mid, content="new wording") is True

        assert [h["id"] for h in memory_index.search("new wording", threshold=0.9)] == [mid]
        assert memory_index.search("old wording", threshold=0.9) == []

    def test_deleted_memory_is_not_found(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("to forget", "facts")
        assert mid is not None

        assert memory_index.delete(mid) is True

        assert memory_index.search("to forget", threshold=0.0) == []

    def test_a_new_model_of_another_width_is_refused_until_rebuilt(self, sync_db: Any) -> None:
        old = MemoryIndex(sync_db, USER, embedder=HashEmbedding(16))
        mid = old.store("kept across models", "facts")
        new_model = HashEmbedding(24, name="wider-model")
        new = MemoryIndex(sync_db, USER, embedder=new_model)

        with pytest.raises(EmbeddingDimensionMismatch, match="rebuild"):
            new.search("kept across models")
        with pytest.raises(EmbeddingDimensionMismatch):
            new.store("another fact", "facts")

        assert new.rebuild() == 1
        assert new_model.calls[-1] == ["kept across models"]
        assert [h["id"] for h in new.search("kept across models", threshold=0.9)] == [mid]
        # The refused memory was not kept.
        assert [m["content"] for m in new.list_all()] == ["kept across models"]


class TestConversationVectorIndexOverTheStore:
    def test_personal_mode_uses_the_sqlite_store(self, sync_db: Any, conversation: int) -> None:
        index = ConversationVectorIndex(sync_db, conversation, USER, embedder=HashEmbedding())

        assert isinstance(index.vector_store, SqliteVectorStore)
        assert index.vector_store.collection is CONTEXT

    def test_index_and_search(self, sync_db: Any, conversation: int) -> None:
        index = ConversationVectorIndex(sync_db, conversation, USER, embedder=HashEmbedding())
        eid = index.index_element("user", "where is the example report")

        hits = index.search("where is the example report", threshold=0.9)

        assert [h["id"] for h in hits] == [eid]

    def test_rebuild_reembeds_the_users_context(self, sync_db: Any, conversation: int) -> None:
        old = ConversationVectorIndex(sync_db, conversation, USER, embedder=HashEmbedding(16))
        eid = old.index_element("assistant", "an answer")
        new = ConversationVectorIndex(sync_db, conversation, USER, embedder=HashEmbedding(24))

        with pytest.raises(EmbeddingDimensionMismatch):
            new.search("an answer")
        assert new.rebuild() == 1
        assert [h["id"] for h in new.search("an answer", threshold=0.9)] == [eid]
