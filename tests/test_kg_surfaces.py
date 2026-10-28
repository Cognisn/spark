"""Auto-context must inject the relevant subgraph into the system prompt."""

from __future__ import annotations

import pytest

from spark.knowledge import store
from tests.test_kg_query import DirectionalEmbedder


@pytest.fixture()
def db(sync_db):
    """The bridge (SyncDatabase) to a fresh migrated SQLite database."""
    return sync_db


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    import spark.knowledge.tools as kg_tools

    monkeypatch.setattr(kg_tools, "_get_embedder", lambda: DirectionalEmbedder())


def _make_manager(db):
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver
    from spark.llm.manager import LLMManager
    from tests.test_conversation_manager import StubLLMService

    stub = StubLLMService()
    llm = LLMManager()
    llm.register_provider(stub)  # type: ignore[arg-type]
    mgr = ConversationManager(db, llm, ContextLimitResolver(), max_tool_iterations=5)
    return mgr, stub


class TestAutoContext:
    def _seed_global(self, db) -> None:
        emb = DirectionalEmbedder()
        a = db.call(
            store.upsert_node,
            "global",
            "Spark",
            "project",
            "AI kit",
            emb.encode("spark").tobytes(),
            "u1",
        )
        b = db.call(
            store.upsert_node,
            "global",
            "Cognisn",
            "organisation",
            "",
            emb.encode("org").tobytes(),
            "u1",
        )
        db.call(store.upsert_edge, "global", a, b, "belongs_to", "", "u1")

    def test_block_injected_on_match(self, db) -> None:
        self._seed_global(db)
        mgr, stub = _make_manager(db)
        cid = mgr.create_conversation("t", "stub-model", "u1")
        mgr.send_message(cid, "tell me about spark", "u1")
        assert "## Knowledge Graph Context" in stub.last_system
        assert "Cognisn" in stub.last_system

    def test_no_block_when_global_disabled(self, db) -> None:
        self._seed_global(db)
        mgr, stub = _make_manager(db)
        cid = mgr.create_conversation("t", "stub-model", "u1")
        db.execute("UPDATE conversations SET kg_use_global = 0 WHERE id = ?", (cid,))
        mgr.send_message(cid, "tell me about spark", "u1")
        assert "## Knowledge Graph Context" not in stub.last_system

    def test_no_block_when_auto_context_off(self, db) -> None:
        self._seed_global(db)
        mgr, stub = _make_manager(db)
        cid = mgr.create_conversation("t", "stub-model", "u1")
        db.execute("UPDATE conversations SET kg_auto_context = 0 WHERE id = ?", (cid,))
        mgr.send_message(cid, "tell me about spark", "u1")
        assert "## Knowledge Graph Context" not in stub.last_system

    def test_no_block_on_no_match(self, db) -> None:
        self._seed_global(db)
        mgr, stub = _make_manager(db)
        cid = mgr.create_conversation("t", "stub-model", "u1")
        mgr.send_message(cid, "talk about coffee", "u1")
        assert "## Knowledge Graph Context" not in stub.last_system
