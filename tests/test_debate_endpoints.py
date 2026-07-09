"""Endpoint tests for debate creation, state, prompt queueing, and page."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.web.server import create_app


def _mock_settings_get(
    key: str, default: object = None, *, cast: type | None = None
) -> object:
    values = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
        "interface.ssl.enabled": False,
        "providers.anthropic.enabled": False,
        "providers.aws_bedrock.enabled": False,
        "providers.ollama.enabled": False,
        "providers.google_gemini.enabled": False,
        "providers.xai.enabled": False,
    }
    val = values.get(key, default)
    if cast is not None and val is not None:
        val = cast(val)
    return val


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """App with a MagicMock manager backed by a real SQLite connection."""
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)

    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema

    mgr = MagicMock()
    mgr._db = conn

    def _create(name, model_id, user_guid, **kwargs):
        from spark.database import conversations as convdb

        return convdb.create_conversation(conn, name, model_id, user_guid)

    mgr.create_conversation.side_effect = _create
    app.state.conversation_manager = mgr
    app.state.user_guid = "test-user"  # the mock ctx would yield a MagicMock guid
    client = TestClient(app)
    return client


def _auth(client: TestClient) -> None:
    code = client.app.state.auth.generate_code()
    resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
    client.cookies.set("spark_session", resp.cookies.get("spark_session", ""))


DEBATE_PAYLOAD = {
    "name": "Tea vs Coffee",
    "conversation_type": "debate",
    "debate": {
        "topic": "Tea is better than coffee",
        "rounds_mode": "fixed",
        "max_rounds": 2,
        "agents": {
            "pro": {"model_id": "stub-model", "brief": "cite health studies"},
            "con": {"model_id": "stub-model", "brief": None},
            "judge": {"model_id": "stub-model", "brief": None},
        },
    },
}


class TestDebateCreate:
    def test_create_debate_conversation(self, client: TestClient) -> None:
        _auth(client)
        r = client.post("/conversations/api/create", json=DEBATE_PAYLOAD)
        assert r.status_code == 200
        assert r.json()["conversation_type"] == "debate"

        cid = r.json()["id"]
        r = client.get(f"/debate/api/state?conversation_id={cid}")
        data = r.json()
        assert data["config"]["topic"] == "Tea is better than coffee"
        assert data["config"]["state"] == "setup"
        assert set(data["config"]["agents"]) == {"pro", "con", "judge"}

    def test_create_debate_missing_agents_is_400(self, client: TestClient) -> None:
        _auth(client)
        bad = {
            **DEBATE_PAYLOAD,
            "debate": {
                "topic": "T",
                "rounds_mode": "fixed",
                "max_rounds": 1,
                "agents": {},
            },
        }
        assert client.post("/conversations/api/create", json=bad).status_code == 400

    def test_standard_create_unchanged(self, client: TestClient) -> None:
        _auth(client)
        r = client.post(
            "/conversations/api/create", json={"name": "N", "model_id": "stub-model"}
        )
        assert r.status_code == 200
        assert r.json().get("conversation_type", "standard") == "standard"


class TestDebatePrompt:
    def test_prompt_queued_as_turn(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json=DEBATE_PAYLOAD).json()["id"]
        r = client.post(
            "/debate/api/prompt",
            json={"conversation_id": cid, "message": "focus on cost"},
        )
        assert r.status_code == 200 and r.json()["queued"] is True

        state = client.get(f"/debate/api/state?conversation_id={cid}").json()
        assert state["turns"][-1]["turn_type"] == "user_prompt"

    def test_prompt_on_standard_conversation_is_404(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post(
            "/conversations/api/create", json={"name": "N", "model_id": "m"}
        ).json()["id"]
        r = client.post(
            "/debate/api/prompt", json={"conversation_id": cid, "message": "x"}
        )
        assert r.status_code == 404


class TestDebatePage:
    def test_debate_page_renders(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json=DEBATE_PAYLOAD).json()["id"]
        r = client.get(f"/debate/{cid}")
        assert r.status_code == 200 and "debate" in r.text.lower()
