"""Tests for the knowledge graph builder."""

from __future__ import annotations

import numpy as np
import pytest

from spark.database import Database, conversations
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.knowledge import store
from spark.knowledge.builder import (
    KnowledgeGraphBuilder,
    is_building,
    release,
    try_acquire,
)
from tests.test_kg_extractor import ScriptedService, text_response, tool_response


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)
    return conn


class FakeEmbedder:
    def encode(self, text, normalize=True):
        return np.ones(8, dtype=np.float32)


def _conv_with_messages(db, n=3, user="u1") -> int:
    from spark.database import messages as msg_db

    cid = conversations.create_conversation(db, "c", "m", user)
    for i in range(n):
        msg_db.add_message(db, cid, "user", f"Matthew works on Spark, message {i}", 10, user)
    return cid


class TestLocalBuild:
    def test_builds_and_advances_watermark(self, db) -> None:
        cid = _conv_with_messages(db)
        svc = ScriptedService(
            [
                tool_response(
                    [
                        {"name": "Matthew", "entity_type": "person", "description": "author"},
                        {"name": "Spark", "entity_type": "project", "description": "kit"},
                    ],
                    [
                        {
                            "source": "Matthew",
                            "target": "Spark",
                            "relation": "works_on",
                            "description": "",
                        }
                    ],
                )
            ]
        )
        b = KnowledgeGraphBuilder(db, lambda m: svc, FakeEmbedder())
        result = b.build_scope(f"conv:{cid}", "u1", "m")
        assert result["status"] == "complete" and result["entities"] == 2
        assert store.count_nodes(db, f"conv:{cid}", "u1") == 2
        assert store.count_edges(db, f"conv:{cid}", "u1") == 1
        state = store.get_build_state(db, f"conv:{cid}", "u1")
        assert state and state[0]["watermark"] > 0

        # Incremental: nothing new -> zero chunks, no further LLM calls
        result2 = b.build_scope(f"conv:{cid}", "u1", "m")
        assert result2["chunks"] == 0 and svc.calls == 1

    def test_skipped_chunk_does_not_advance_watermark(self, db) -> None:
        cid = _conv_with_messages(db)
        svc = ScriptedService([text_response(), text_response()])  # both attempts fail
        b = KnowledgeGraphBuilder(db, lambda m: svc, FakeEmbedder())
        result = b.build_scope(f"conv:{cid}", "u1", "m")
        assert result["skipped_chunks"] == 1
        state = store.get_build_state(db, f"conv:{cid}", "u1")
        assert not state or state[0]["watermark"] == 0


class TestGlobalBuild:
    def test_global_covers_conversations_and_memories(self, db) -> None:
        from spark.database import memories as mem_db

        cid = _conv_with_messages(db)
        mem_db.add_memory(
            db,
            "u1",
            "Prefers Australian English",
            "preferences",
            np.ones(8, dtype=np.float32),
        )
        responses = [tool_response([{"name": f"E{i}"}], []) for i in range(4)]
        svc = ScriptedService(responses)
        b = KnowledgeGraphBuilder(db, lambda m: svc, FakeEmbedder())
        result = b.build_scope("global", "u1", "m")
        assert result["status"] == "complete"
        state = {s["source_key"] for s in store.get_build_state(db, "global", "u1")}
        assert f"conv:{cid}" in state and "memories" in state

    def test_plan_scope_counts_pending(self, db) -> None:
        cid = _conv_with_messages(db)
        b = KnowledgeGraphBuilder(db, lambda m: None, FakeEmbedder())
        plan = b.plan_scope(f"conv:{cid}", "u1")
        assert plan["pending_chunks"] >= 1


class TestCancellation:
    def test_cancel_between_chunks(self, db) -> None:
        from spark.core.cancellation import CancellationToken

        cid = _conv_with_messages(db)
        token = CancellationToken()
        token.cancel("test")
        svc = ScriptedService([])
        b = KnowledgeGraphBuilder(db, lambda m: svc, FakeEmbedder())
        result = b.build_scope(f"conv:{cid}", "u1", "m", cancel_token=token)
        assert result["status"] == "cancelled" and svc.calls == 0


class TestLock:
    def test_lock_lifecycle(self) -> None:
        assert try_acquire("conv:99") is True
        assert is_building("conv:99") is True
        assert try_acquire("conv:99") is False
        release("conv:99")
        assert is_building("conv:99") is False
