"""End to end: build a conversation graph, query it, and pool it via a link."""

from __future__ import annotations

import pytest

from spark.database import Database, conversation_links, conversations
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.knowledge.builder import KnowledgeGraphBuilder
from spark.knowledge.resolve import available_scopes
from spark.knowledge.tools import execute
from tests.test_kg_extractor import ScriptedService, tool_response
from tests.test_kg_query import DirectionalEmbedder


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)
    return conn


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    import spark.knowledge.tools as kg_tools

    monkeypatch.setattr(kg_tools, "_get_embedder", lambda: DirectionalEmbedder())


def test_build_query_and_link_pooling(db) -> None:
    from spark.database import messages as msg_db

    # 1. A conversation with graph-worthy content and a local graph enabled
    source = conversations.create_conversation(db, "research", "stub-model", "u1")
    msg_db.add_message(db, source, "user", "Spark is developed by Cognisn", 10, "u1")
    ph = db.placeholder
    db.execute(f"UPDATE conversations SET kg_local_enabled = 1 WHERE id = {ph}", (source,))
    db.commit()

    # 2. Build the local graph with a scripted extraction (embedder maps 'spark')
    svc = ScriptedService(
        [
            tool_response(
                [
                    {"name": "Spark", "entity_type": "project", "description": "AI kit"},
                    {"name": "Cognisn", "entity_type": "organisation", "description": ""},
                ],
                [
                    {
                        "source": "Spark",
                        "target": "Cognisn",
                        "relation": "developed_by",
                        "description": "",
                    }
                ],
            )
        ]
    )
    builder = KnowledgeGraphBuilder(db, lambda m: svc, DirectionalEmbedder())
    result = builder.build_scope(f"conv:{source}", "u1", "stub-model")
    assert result["status"] == "complete"

    # 3. The source conversation sees its own graph
    assert f"conv:{source}" in available_scopes(db, source, "u1")

    # 4. The query tool renders the relationship
    text, is_error = execute(
        "query_knowledge_graph",
        {"query": "spark"},
        {"_kg_db": db, "_kg_conversation_id": source, "_kg_user_guid": "u1"},
    )
    assert not is_error and "developed_by" in text and "Cognisn" in text

    # 5. A second conversation pools the graph through a link
    other = conversations.create_conversation(db, "follow-up", "stub-model", "u1")
    assert f"conv:{source}" not in available_scopes(db, other, "u1")
    conversation_links.add_link(db, other, source, "u1")
    assert f"conv:{source}" in available_scopes(db, other, "u1")

    # 6. Auto-context in the linked conversation carries the pooled entities
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver
    from spark.llm.manager import LLMManager
    from tests.test_conversation_manager import StubLLMService

    stub = StubLLMService()
    llm = LLMManager()
    llm.register_provider(stub)  # type: ignore[arg-type]
    mgr = ConversationManager(db, llm, ContextLimitResolver(), max_tool_iterations=5)
    mgr.send_message(other, "what do we know about spark?", "u1")
    assert "## Knowledge Graph Context" in stub.last_system
    assert "Cognisn" in stub.last_system
