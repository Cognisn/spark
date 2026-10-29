"""End-to-end panel flow: create, run to the human pause, resume, QA, export."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.core.panel.orchestrator import PanelOrchestrator
from spark.database import debates
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_debate_orchestrator import ScriptedService, text_response, tool_response
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
        app.state.user_guid = "test-user"
        yield client


def _auth(client: TestClient) -> None:
    sign_in(client)


def test_full_panel_with_human(client: TestClient) -> None:
    _auth(client)

    # 1. Create a two-AI panel with the user as the third panellist.
    payload = {
        "name": "Housing panel",
        "conversation_type": "panel",
        "panel": {
            "topic": "How do we fix housing affordability?",
            "rounds_mode": "fixed",
            "max_rounds": 1,
            "moderator": {"model_id": "model-mod", "brief": None},
            "panellists": [
                {"name": "Economist", "model_id": "model-p1", "brief": "economist"},
                {"name": "Planner", "model_id": "model-p2", "brief": "urban planner"},
            ],
            "human": {"name": "Matthew"},
        },
    }
    cid = client.post("/conversations/api/create", json=payload).json()["id"]
    db = client.app.state.conversation_manager._db

    # 2. Run the orchestrator with scripted services: human speaks second.
    services = {
        "model-mod": ScriptedService(
            [
                tool_response(
                    "set_speaking_order",
                    {"order": ["panellist:1", "panellist:3", "panellist:2"]},
                    "Welcome to the panel.",
                ),
                text_response("Synthesis: supply and lived experience both matter."),
            ]
        ),
        "model-p1": ScriptedService(
            [
                tool_response(
                    "submit_contribution",
                    {
                        "contribution_markdown": "Zoning reform first.",
                        "exhibits": [{"title": "Supply data", "content": "Numbers"}],
                    },
                )
            ]
        ),
        "model-p2": ScriptedService(
            [tool_response("submit_contribution", {"contribution_markdown": "Density done well."})]
        ),
    }
    client.app.state.conversation_manager._get_llm_service_for_model = lambda m: services[m]
    orch = PanelOrchestrator(db, lambda m: services[m], {})
    orch.run(cid, "test-user")

    # Paused at the human's slot, surfaced through the state endpoint.
    state = client.get(f"/panel/api/state?conversation_id={cid}").json()
    assert state["config"]["state"] == "round"
    assert state["awaiting_human"]["name"] == "Matthew"

    # 3. The human submits their contribution through the prompt endpoint.
    r = client.post(
        "/panel/api/prompt",
        json={"conversation_id": cid, "message": "Renters need security of tenure."},
    )
    assert r.json()["accepted"] == "contribution"

    # 4. Resume: the round completes and the moderator synthesises.
    orch.run(cid, "test-user")
    cfg = db.call(debates.get_debate, cid)
    assert cfg["state"] == "qa"
    contribs = [
        t["role"] for t in db.call(debates.get_turns, cid) if t["turn_type"] == "contribution"
    ]
    assert contribs == ["panellist:1", "panellist:3", "panellist:2"]

    # 5. QA: the prompt endpoint answers synchronously via the moderator.
    services["model-mod"].responses.append(text_response("The panel leant to supply."))
    r = client.post("/panel/api/prompt", json={"conversation_id": cid, "message": "Who won?"})
    assert r.json() == {"queued": False, "answer": "The panel leant to supply."}

    # 6. Export renders names and the synthesis.
    mgr = client.app.state.conversation_manager
    mgr.get_conversation.return_value = {
        "id": cid,
        "name": "Housing panel",
        "conversation_type": "panel",
    }
    mgr.get_messages.return_value = []
    md = client.get(f"/chat/{cid}/api/export?format=markdown").text
    assert "# Panel: How do we fix housing affordability?" in md
    assert "### Economist" in md
    assert "### Matthew" in md
    assert "Renters need security of tenure." in md
    assert "## Synthesis" in md
    html = client.get(f"/chat/{cid}/api/export?format=html").text
    assert "<h3>Planner</h3>" in html
    assert "<h2>Synthesis</h2>" in html
