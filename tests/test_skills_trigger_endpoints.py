"""The /skill-name trigger must rewrite messages on both streaming surfaces."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.skills.manager import SkillsManager, set_skills_manager
from spark.skills.trigger import resolve_trigger_for_conversation
from spark.web.server import create_app
from tests.test_skills_manager import make_skill


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


@pytest.fixture()
def skills_env(tmp_path):
    make_skill(tmp_path / "user", "pdf-filler")
    m = SkillsManager(tmp_path / "user")
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture()
def client(db) -> TestClient:
    """App with a MagicMock manager backed by the real SQLite connection."""
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)

    mgr = MagicMock()
    mgr._db = db

    def _create(name, model_id, user_guid, **kwargs):
        from spark.database import conversations as convdb

        return convdb.create_conversation(db, name, model_id, user_guid)

    mgr.create_conversation.side_effect = _create
    app.state.conversation_manager = mgr
    app.state.user_guid = "test-user"
    return TestClient(app)


def _auth(client: TestClient) -> None:
    code = client.app.state.auth.generate_code()
    resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
    client.cookies.set("spark_session", resp.cookies.get("spark_session", ""))


DEBATE_PAYLOAD = {
    "name": "T",
    "conversation_type": "debate",
    "debate": {
        "topic": "Topic",
        "rounds_mode": "fixed",
        "max_rounds": 1,
        "agents": {
            "pro": {"model_id": "m", "brief": None},
            "con": {"model_id": "m", "brief": None},
            "judge": {"model_id": "m", "brief": None},
        },
    },
}


class TestResolver:
    def test_rewrites_enabled_match(self, skills_env, db) -> None:
        out = resolve_trigger_for_conversation(db, "u1", None, "/pdf-filler go")
        assert "use_skill('pdf-filler')" in out

    def test_disabled_skill_not_triggered(self, skills_env, db) -> None:
        from spark.database import skills as skills_db

        skills_db.set_skill_enabled(db, "pdf-filler", False, "u1")
        out = resolve_trigger_for_conversation(db, "u1", None, "/pdf-filler go")
        assert out == "/pdf-filler go"

    def test_failure_safe(self, skills_env) -> None:
        out = resolve_trigger_for_conversation(None, "u1", None, "/pdf-filler go")
        assert out == "/pdf-filler go"


class TestDebatePromptTrigger:
    def test_debate_prompt_rewritten(self, skills_env, client) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json=DEBATE_PAYLOAD).json()["id"]
        client.post(
            "/debate/api/prompt",
            json={"conversation_id": cid, "message": "/pdf-filler check the form"},
        )
        state = client.get(f"/debate/api/state?conversation_id={cid}").json()
        assert "use_skill('pdf-filler')" in state["turns"][-1]["content"]


class TestChatStreamTrigger:
    def test_stream_chat_passes_rewritten_message(self, skills_env, client) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json={"name": "n", "model_id": "m"}).json()[
            "id"
        ]

        captured: dict = {}
        conv_mgr = client.app.state.conversation_manager

        def record_send(conversation_id, user_message, user_guid, **kwargs):
            captured["message"] = user_message
            return "ok"

        conv_mgr.send_message.side_effect = record_send
        with client.stream(
            "GET", f"/stream/chat?conversation_id={cid}&message=/pdf-filler go"
        ) as r:
            for _ in r.iter_lines():
                if captured:
                    break
        assert "use_skill('pdf-filler')" in captured["message"]
