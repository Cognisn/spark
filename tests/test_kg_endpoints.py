"""Knowledge endpoint tests: build lifecycle, status, graph JSON, clear."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import Database, conversations
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.knowledge import store
from spark.web.server import create_app
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


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture()
def client(db) -> TestClient:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)

    svc = ScriptedService(
        [
            tool_response(
                [
                    {"name": "Spark", "entity_type": "project", "description": "kit"},
                    {"name": "Matthew", "entity_type": "person", "description": ""},
                ],
                [{"source": "Matthew", "target": "Spark", "relation": "works_on",
                  "description": ""}],
            )
            for _ in range(10)
        ]
    )
    mgr = MagicMock()
    mgr._db = db
    mgr._get_llm_service_for_model.side_effect = lambda m: svc
    app.state.conversation_manager = mgr
    app.state.user_guid = "test-user"
    return TestClient(app)


def _auth(client: TestClient) -> None:
    code = client.app.state.auth.generate_code()
    resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
    client.cookies.set("spark_session", resp.cookies.get("spark_session", ""))


def _seed_conversation(db) -> int:
    from spark.database import messages as msg_db

    cid = conversations.create_conversation(db, "c", "m", "test-user")
    msg_db.add_message(db, cid, "user", "Matthew works on Spark", 10, "test-user")
    return cid


class TestBuildLifecycle:
    def test_build_then_status_then_graph(self, client, db) -> None:
        _auth(client)
        _seed_conversation(db)
        r = client.post("/knowledge/api/build", json={"scope": "global"})
        assert r.status_code == 200

        for _ in range(50):
            status = client.get("/knowledge/api/status").json()
            if not status["scopes"]["global"]["building"]:
                break
            time.sleep(0.1)
        assert status["scopes"]["global"]["nodes"] >= 2

        g = client.get("/knowledge/api/graph?scope=global").json()
        assert g["total_nodes"] >= 2
        assert "embedding" not in g["nodes"][0]
        assert g["edges"]

    def test_conflict_while_building(self, client) -> None:
        _auth(client)
        from spark.knowledge import builder

        assert builder.try_acquire("global")
        try:
            assert (
                client.post("/knowledge/api/build", json={"scope": "global"}).status_code
                == 409
            )
        finally:
            builder.release("global")

    def test_unknown_scope_graph_is_empty(self, client) -> None:
        _auth(client)
        g = client.get("/knowledge/api/graph?scope=conv:999").json()
        assert g["total_nodes"] == 0


class TestSearchAndClear:
    def test_search(self, client, db) -> None:
        _auth(client)
        emb = DirectionalEmbedder()
        store.upsert_node(
            db, "global", "Spark", "project", "kit", emb.encode("spark").tobytes(),
            "test-user",
        )
        r = client.get("/knowledge/api/search?q=spark&scope=global").json()
        assert r["results"] and r["results"][0]["name"] == "Spark"

    def test_clear_and_page(self, client, db) -> None:
        _auth(client)
        store.upsert_node(db, "global", "X", "concept", "", None, "test-user")
        assert client.post("/knowledge/api/clear", json={"scope": "global"}).status_code == 200
        assert store.count_nodes(db, "global", "test-user") == 0
        assert "knowledge" in client.get("/knowledge").text.lower()
