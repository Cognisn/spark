"""query_knowledge_graph tool tests."""

from __future__ import annotations

import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.knowledge import store
from spark.knowledge.tools import execute, get_tools
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


def test_tool_shape() -> None:
    tools = get_tools()
    assert tools[0]["name"] == "query_knowledge_graph"
    assert "query" in tools[0]["inputSchema"]["required"]


class TestExecute:
    def test_no_graphs_message(self, db) -> None:
        text, is_error = execute(
            "query_knowledge_graph",
            {"query": "spark"},
            {"_kg_db": db, "_kg_conversation_id": None, "_kg_user_guid": "u1"},
        )
        assert not is_error and "Knowledge page" in text

    def test_query_global(self, db) -> None:
        emb = DirectionalEmbedder()
        a = store.upsert_node(
            db, "global", "Spark", "project", "AI kit", emb.encode("spark").tobytes(), "u1"
        )
        b = store.upsert_node(
            db, "global", "Cognisn", "organisation", "", emb.encode("org").tobytes(), "u1"
        )
        store.upsert_edge(db, "global", a, b, "belongs_to", "", "u1")
        text, is_error = execute(
            "query_knowledge_graph",
            {"query": "spark"},
            {"_kg_db": db, "_kg_conversation_id": None, "_kg_user_guid": "u1"},
        )
        assert not is_error and "Cognisn" in text

    def test_no_match_is_helpful(self, db) -> None:
        emb = DirectionalEmbedder()
        store.upsert_node(
            db, "global", "Spark", "project", "AI kit", emb.encode("spark").tobytes(), "u1"
        )
        text, is_error = execute(
            "query_knowledge_graph",
            {"query": "coffee"},
            {"_kg_db": db, "_kg_conversation_id": None, "_kg_user_guid": "u1"},
        )
        assert not is_error and "no entities matched" in text.lower()

    def test_missing_context_is_safe(self) -> None:
        text, is_error = execute("query_knowledge_graph", {"query": "x"}, {})
        assert not is_error and "unavailable" in text.lower()


class TestRegistry:
    def test_offered_and_dispatched(self, db) -> None:
        from spark.tools.registry import execute_builtin_tool, get_builtin_tools

        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert "query_knowledge_graph" in names
        text, is_error = execute_builtin_tool(
            "query_knowledge_graph",
            {"query": "x"},
            {"_kg_db": db, "_kg_conversation_id": None, "_kg_user_guid": "u1"},
        )
        assert not is_error
