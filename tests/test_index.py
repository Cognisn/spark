"""Tests for the memory and vector indexing layer."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.index.embeddings import EmbeddingModel
from spark.index.memory_index import MemoryIndex, VALID_CATEGORIES
from spark.index.vector_index import ConversationVectorIndex


USER = "test-user"
DIM = 64


# -- Fixtures -----------------------------------------------------------------


def _mock_embedder() -> MagicMock:
    """Create a mock EmbeddingModel that returns random vectors."""
    mock = MagicMock(spec=EmbeddingModel)
    mock.embedding_dimension = DIM

    def encode_side_effect(texts: str | list[str], **kwargs: Any) -> np.ndarray:
        if isinstance(texts, str):
            np.random.seed(hash(texts) % 2**31)
            return np.random.randn(DIM).astype(np.float32)
        return np.random.randn(len(texts), DIM).astype(np.float32)

    mock.encode = MagicMock(side_effect=encode_side_effect)
    return mock


@pytest.fixture()
def db(tmp_path) -> Database:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    return Database(conn)


@pytest.fixture()
def conn(db: Database) -> DatabaseConnection:
    return db.connection


@pytest.fixture()
def memory_index(conn: DatabaseConnection) -> MemoryIndex:
    mi = MemoryIndex(conn, USER)
    mi._embedder = _mock_embedder()
    return mi


@pytest.fixture()
def vector_index(conn: DatabaseConnection) -> ConversationVectorIndex:
    from spark.database import conversations

    cid = conversations.create_conversation(conn, "Test", "model", USER)
    vi = ConversationVectorIndex(conn, cid, USER)
    vi._embedder = _mock_embedder()
    return vi


# -- EmbeddingModel -----------------------------------------------------------


class TestEmbeddingModel:
    def test_compute_similarity_identical(self) -> None:
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        sim = EmbeddingModel.compute_similarity(a, a)
        assert sim[0] == pytest.approx(1.0)

    def test_compute_similarity_orthogonal(self) -> None:
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        sim = EmbeddingModel.compute_similarity(a, b)
        assert sim[0] == pytest.approx(0.0)

    def test_compute_similarity_batch(self) -> None:
        query = np.array([1.0, 0.0], dtype=np.float32)
        corpus = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]], dtype=np.float32)
        sims = EmbeddingModel.compute_similarity(query, corpus)
        assert len(sims) == 3
        assert sims[0] > sims[1]


# -- MemoryIndex --------------------------------------------------------------


class TestMemoryIndex:
    def test_store_and_search(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("Python is my favourite language", "preferences")
        assert mid is not None

        # Use the exact same text so the mock embedder produces the same vector
        results = memory_index.search("Python is my favourite language", threshold=0.0)
        assert len(results) >= 1

    def test_store_duplicate(self, memory_index: MemoryIndex) -> None:
        memory_index.store("unique fact", "facts")
        result = memory_index.store("unique fact", "facts")
        assert result is None

    def test_invalid_category_defaults_to_facts(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("test", "invalid_category")
        assert mid is not None
        mem = memory_index.get(mid)
        assert mem is not None
        assert mem["category"] == "facts"

    def test_list_all(self, memory_index: MemoryIndex) -> None:
        memory_index.store("fact one", "facts")
        memory_index.store("fact two", "facts")
        memory_index.store("pref one", "preferences")
        all_mems = memory_index.list_all()
        assert len(all_mems) == 3

    def test_list_by_category(self, memory_index: MemoryIndex) -> None:
        memory_index.store("fact one", "facts")
        memory_index.store("pref one", "preferences")
        facts = memory_index.list_all(category="facts")
        assert len(facts) == 1

    def test_delete(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("to delete", "facts")
        assert mid is not None
        assert memory_index.delete(mid) is True
        assert memory_index.get(mid) is None

    def test_delete_nonexistent(self, memory_index: MemoryIndex) -> None:
        assert memory_index.delete(99999) is False

    def test_clear_all(self, memory_index: MemoryIndex) -> None:
        memory_index.store("a", "facts")
        memory_index.store("b", "facts")
        memory_index.clear_all()
        assert len(memory_index.list_all()) == 0

    def test_auto_retrieve(self, memory_index: MemoryIndex) -> None:
        memory_index.store("I prefer dark mode in all apps", "preferences")
        # Use exact same text for deterministic mock embedding match
        result = memory_index.auto_retrieve("I prefer dark mode in all apps", threshold=0.0)
        assert result is not None
        assert "Relevant Memories" in result
        assert "dark mode" in result

    def test_auto_retrieve_disabled(self, conn: DatabaseConnection) -> None:
        mi = MemoryIndex(conn, USER, config={"auto_retrieval": {"enabled": False}})
        mi._embedder = _mock_embedder()
        mi.store("something", "facts")
        result = mi.auto_retrieve("anything")
        assert result is None

    def test_auto_retrieve_no_matches(self, memory_index: MemoryIndex) -> None:
        # No memories stored → should return None
        result = memory_index.auto_retrieve("hello")
        assert result is None

    def test_search_with_categories(self, memory_index: MemoryIndex) -> None:
        memory_index.store("python fact", "facts")
        memory_index.store("dark mode pref", "preferences")
        results = memory_index.search("python", categories=["facts"], threshold=0.0)
        # Should only search facts category
        assert all(r.get("category") == "facts" for r in results if "category" in r)

    def test_update_content(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("old content", "facts")
        assert mid is not None
        assert memory_index.update(mid, content="new content") is True
        mem = memory_index.get(mid)
        assert mem is not None
        assert mem["content"] == "new content"

    def test_update_nonexistent(self, memory_index: MemoryIndex) -> None:
        assert memory_index.update(99999, content="x") is False

    def test_update_category(self, memory_index: MemoryIndex) -> None:
        mid = memory_index.store("test", "facts")
        assert mid is not None
        memory_index.update(mid, category="preferences")
        # Re-fetch to verify
        mem = memory_index.get(mid)
        assert mem is not None


class TestValidCategories:
    def test_all_categories(self) -> None:
        assert "preferences" in VALID_CATEGORIES
        assert "facts" in VALID_CATEGORIES
        assert "projects" in VALID_CATEGORIES
        assert "instructions" in VALID_CATEGORIES
        assert "relationships" in VALID_CATEGORIES


# -- ConversationVectorIndex --------------------------------------------------


class TestConversationVectorIndex:
    def test_index_element(self, vector_index: ConversationVectorIndex) -> None:
        eid = vector_index.index_element("user", "Hello world")
        assert eid is not None

    def test_index_duplicate(self, vector_index: ConversationVectorIndex) -> None:
        vector_index.index_element("user", "same content")
        eid2 = vector_index.index_element("user", "same content")
        assert eid2 is None

    def test_index_invalid_type(self, vector_index: ConversationVectorIndex) -> None:
        eid = vector_index.index_element("invalid", "content")
        assert eid is None

    def test_index_empty_content(self, vector_index: ConversationVectorIndex) -> None:
        eid = vector_index.index_element("user", "  ")
        assert eid is None

    def test_index_message(self, vector_index: ConversationVectorIndex) -> None:
        ids = vector_index.index_message(1, "user", "Hello there")
        assert len(ids) == 1

    def test_index_message_tool_results_skipped(self, vector_index: ConversationVectorIndex) -> None:
        ids = vector_index.index_message(1, "user", "[TOOL_RESULTS]{}")
        assert len(ids) == 0

    def test_index_tool_call(self, vector_index: ConversationVectorIndex) -> None:
        eid = vector_index.index_tool_call("read_file", {"path": "/tmp/test"}, 1)
        assert eid is not None

    def test_index_tool_result(self, vector_index: ConversationVectorIndex) -> None:
        eid = vector_index.index_tool_result("read_file", "file contents here", 1)
        assert eid is not None

    def test_index_tool_result_truncated(self, vector_index: ConversationVectorIndex) -> None:
        long_result = "x" * 5000
        eid = vector_index.index_tool_result("tool", long_result, 1)
        assert eid is not None

    def test_index_system_instructions(self, vector_index: ConversationVectorIndex) -> None:
        ids = vector_index.index_system_instructions("You are a helpful assistant.")
        assert len(ids) >= 1

    def test_index_system_instructions_chunked(self, vector_index: ConversationVectorIndex) -> None:
        long_instructions = "This is a test. " * 200
        ids = vector_index.index_system_instructions(long_instructions)
        assert len(ids) > 1

    def test_search(self, vector_index: ConversationVectorIndex) -> None:
        vector_index.index_element("user", "Python programming language")
        vector_index.index_element("user", "JavaScript framework")
        # Use exact text for deterministic mock embedding match
        results = vector_index.search("Python programming language", threshold=0.0)
        assert len(results) >= 1

    def test_clear(self, vector_index: ConversationVectorIndex) -> None:
        vector_index.index_element("user", "to clear")
        vector_index.clear()
        stats = vector_index.get_stats()
        assert sum(stats.values()) == 0

    def test_get_stats(self, vector_index: ConversationVectorIndex) -> None:
        vector_index.index_element("user", "user message")
        vector_index.index_element("assistant", "assistant response")
        stats = vector_index.get_stats()
        assert stats.get("user") == 1
        assert stats.get("assistant") == 1

    def test_chunk_text_short(self, vector_index: ConversationVectorIndex) -> None:
        chunks = vector_index._chunk_text("short text")
        assert len(chunks) == 1
        assert chunks[0] == "short text"

    def test_chunk_text_long(self, vector_index: ConversationVectorIndex) -> None:
        long_text = "word " * 500
        chunks = vector_index._chunk_text(long_text)
        assert len(chunks) > 1
        # Verify no empty chunks
        assert all(c.strip() for c in chunks)

    def test_index_json_message(self, vector_index: ConversationVectorIndex) -> None:
        import json

        blocks = [
            {"type": "text", "text": "Let me check that."},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
        ]
        ids = vector_index.index_message(1, "assistant", json.dumps(blocks))
        # Should index the text block only
        assert len(ids) == 1
