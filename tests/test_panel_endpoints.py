"""Endpoint tests for panel creation, state, prompt routing, and page."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import debates
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_debate_orchestrator import ScriptedService, text_response
from tests.web_helpers import sign_in


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
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
def client(sqlite_settings) -> Iterator[TestClient]:
    """App with a MagicMock manager backed by a real SQLite database."""
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)

    with app_client(app, sqlite_settings) as client:
        sync_db = app.state.sync_db
        mgr = MagicMock()
        mgr._db = sync_db

        def _create(name, model_id, user_guid, **kwargs):
            from spark.database import conversations as convdb

            return sync_db.call(convdb.create_conversation, name, model_id, user_guid)

        mgr.create_conversation.side_effect = _create
        app.state.conversation_manager = mgr
        app.state.user_guid = "test-user"  # the mock ctx would yield a MagicMock guid
        yield client


def _auth(client: TestClient) -> None:
    sign_in(client)


PANEL_PAYLOAD = {
    "name": "Future of work",
    "conversation_type": "panel",
    "panel": {
        "topic": "How will AI change knowledge work?",
        "rounds_mode": "fixed",
        "max_rounds": 2,
        "moderator": {"model_id": "stub-model", "brief": None},
        "panellists": [
            {"name": "Economist", "model_id": "stub-model", "brief": "labour economist"},
            {"name": "Engineer", "model_id": "stub-model", "brief": None},
        ],
        "human": None,
    },
}


class TestPanelCreate:
    def test_create_panel_conversation(self, client: TestClient) -> None:
        _auth(client)
        r = client.post("/conversations/api/create", json=PANEL_PAYLOAD)
        assert r.status_code == 200
        assert r.json()["conversation_type"] == "panel"

        cid = r.json()["id"]
        r = client.get(f"/panel/api/state?conversation_id={cid}")
        data = r.json()
        assert data["config"]["topic"] == "How will AI change knowledge work?"
        assert data["config"]["state"] == "setup"
        agents = data["config"]["agents"]
        assert set(agents) == {"moderator", "panellist:1", "panellist:2"}
        assert agents["panellist:1"]["display_name"] == "Economist"

    def test_create_panel_with_human(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["human"] = {"name": "Matthew"}
        cid = client.post("/conversations/api/create", json=payload).json()["id"]
        agents = client.get(f"/panel/api/state?conversation_id={cid}").json()["config"]["agents"]
        assert agents["panellist:3"]["display_name"] == "Matthew"
        assert agents["panellist:3"]["is_human"] is True

    def test_too_few_panellists_is_400(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["panellists"] = payload["panel"]["panellists"][:1]
        assert client.post("/conversations/api/create", json=payload).status_code == 400

    def test_too_many_panellists_is_400(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["panellists"] = [{"name": f"P{i}", "model_id": "m"} for i in range(6)]
        assert client.post("/conversations/api/create", json=payload).status_code == 400

    def test_duplicate_names_is_400(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["human"] = {"name": "economist"}  # clashes case-insensitively
        assert client.post("/conversations/api/create", json=payload).status_code == 400

    def test_missing_model_is_400(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["panellists"][0]["model_id"] = ""
        assert client.post("/conversations/api/create", json=payload).status_code == 400

    def test_invalid_allowlist_type_is_400(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["panellists"][0]["allowed_tools"] = "web_search"
        assert client.post("/conversations/api/create", json=payload).status_code == 400

    def test_allowlists_persist(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["panellists"][0]["allowed_tools"] = ["web_search"]
        payload["panel"]["moderator"]["allowed_skills"] = []
        cid = client.post("/conversations/api/create", json=payload).json()["id"]
        agents = client.get(f"/panel/api/state?conversation_id={cid}").json()["config"]["agents"]
        assert agents["panellist:1"]["allowed_tools"] == ["web_search"]
        assert agents["moderator"]["allowed_skills"] == []


class TestPanelPrompt:
    def test_prompt_queued_as_directive(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json=PANEL_PAYLOAD).json()["id"]
        r = client.post(
            "/panel/api/prompt", json={"conversation_id": cid, "message": "focus on cost"}
        )
        assert r.status_code == 200 and r.json()["queued"] is True
        state = client.get(f"/panel/api/state?conversation_id={cid}").json()
        assert state["turns"][-1]["turn_type"] == "user_prompt"

    def test_prompt_on_standard_conversation_is_404(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json={"name": "N", "model_id": "m"}).json()[
            "id"
        ]
        r = client.post("/panel/api/prompt", json={"conversation_id": cid, "message": "x"})
        assert r.status_code == 404

    def test_prompt_during_human_turn_stores_contribution(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["human"] = {"name": "Matthew"}
        cid = client.post("/conversations/api/create", json=payload).json()["id"]

        db = client.app.state.conversation_manager._db
        db.call(
            debates.update_debate_state,
            cid,
            "round",
            current_round=1,
            opening_speaker=json.dumps(["panellist:3", "panellist:1", "panellist:2"]),
        )
        r = client.post("/panel/api/prompt", json={"conversation_id": cid, "message": "My take."})
        assert r.status_code == 200
        assert r.json()["accepted"] == "contribution"
        turns = client.get(f"/panel/api/state?conversation_id={cid}").json()["turns"]
        last = turns[-1]
        assert last["turn_type"] == "contribution"
        assert last["role"] == "panellist:3"

    def test_prompt_in_qa_answers_synchronously(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json=PANEL_PAYLOAD).json()["id"]
        db = client.app.state.conversation_manager._db
        db.call(debates.update_debate_state, cid, "qa")
        stub = ScriptedService([text_response("Because they agreed.")])
        client.app.state.conversation_manager._get_llm_service_for_model = lambda m: stub
        r = client.post("/panel/api/prompt", json={"conversation_id": cid, "message": "Why?"})
        assert r.status_code == 200
        assert r.json() == {"queued": False, "answer": "Because they agreed."}


class TestPanelPage:
    def test_panel_page_renders(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json=PANEL_PAYLOAD).json()["id"]
        r = client.get(f"/panel/{cid}")
        assert r.status_code == 200 and "panel" in r.text.lower()

    def test_state_on_standard_conversation_is_404(self, client: TestClient) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json={"name": "N", "model_id": "m"}).json()[
            "id"
        ]
        assert client.get(f"/panel/api/state?conversation_id={cid}").status_code == 404


class TestPanelVoices:
    def test_voice_ids_persist_and_human_has_none(self, client: TestClient) -> None:
        _auth(client)
        payload = copy.deepcopy(PANEL_PAYLOAD)
        payload["panel"]["human"] = {"name": "Matthew"}
        payload["panel"]["moderator"]["voice_id"] = "voice-mod"
        payload["panel"]["panellists"][0]["voice_id"] = "voice-one"
        cid = client.post("/conversations/api/create", json=payload).json()["id"]

        agents = client.get(f"/panel/api/state?conversation_id={cid}").json()["config"]["agents"]
        assert agents["moderator"]["voice_id"] == "voice-mod"
        assert agents["panellist:1"]["voice_id"] == "voice-one"
        assert agents["panellist:2"]["voice_id"] is None
        # The human speaks for themselves, so they never get a voice.
        assert agents["panellist:3"]["voice_id"] is None
