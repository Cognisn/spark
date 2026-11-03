"""Server mode: each user's turns, default model and graph builds use that user's model.

The real app runs on a PostgreSQL clone at head with synthetic users signed in
through warden (the fixtures of ``tests.test_user_scoping_web``), over the shared
LLM manager of ``tests.test_models_offered_web``.

* Two users' chat turns at once on different models each answer with their own,
  and the shared manager's model never moves.
* The model list (``/conversations/api/models``) names the signed-in user's
  default model preference, only when it is offered, never ``config.yaml``'s.
* A knowledge graph build without ``knowledge_graph.build_model`` uses the
  user's default model preference, never whatever the shared manager last had.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations
from spark.database import messages as msg_db
from tests.identity_web_helpers import another_client, sign_in
from tests.test_kg_extractor import ScriptedService, tool_response
from tests.test_kg_query import DirectionalEmbedder
from tests.test_models_offered_web import (  # noqa: F401 - fixtures
    ADA,
    GONE,
    GRACE,
    OFFERED,
    ROOT,
    OllamaStandIn,
    shared,
)
from tests.test_user_scoping_web import (  # noqa: F401 - fixtures
    _call,
    _signed_in,
    client,
    host,
    server_db,
)

pytestmark = pytest.mark.postgres


def _plain_conversation(client: TestClient, guid: str, model: str) -> int:
    """A conversation with no memory or retrieval (no embedding model needed)."""
    cid: int = _call(client, conversations.create_conversation, f"{guid}'s", model, guid)
    _call(client, conversations.update_conversation, cid, guid, memory_enabled=0, rag_enabled=0)
    return cid


def _root(client: TestClient) -> TestClient:
    """The administrator the ``shared`` fixture made, signed in on another browser."""
    browser = another_client(client)
    assert sign_in(browser, ROOT).status_code == 303
    return browser


def _prefer(browser: TestClient, model: str) -> None:
    saved = browser.post("/settings/api/preferences", json={"default_model.model_id": model})
    assert saved.status_code == 200, saved.text


@pytest.mark.usefixtures("shared")
class TestConcurrentTurns:
    def test_each_user_answers_with_their_own_model(
        self, client: TestClient, shared: OllamaStandIn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ada, ada_guid = _signed_in(client, ADA)
        grace, grace_guid = _signed_in(client, GRACE)
        chosen = {ada_guid: (ada, "model-a"), grace_guid: (grace, "model-b")}
        cids = {guid: _plain_conversation(client, guid, m) for guid, (_, m) in chosen.items()}
        barrier = threading.Barrier(2, timeout=30)
        invoke = OllamaStandIn.invoke_model

        def held(self: OllamaStandIn, messages: list[dict], **kwargs: Any) -> dict:
            barrier.wait()  # both turns have chosen their model before either answers
            return invoke(self, messages, **kwargs)

        monkeypatch.setattr(OllamaStandIn, "invoke_model", held)
        answers: dict[str, Any] = {}

        def turn(guid: str) -> None:
            browser = chosen[guid][0]
            answers[guid] = browser.post(f"/chat/{cids[guid]}/api/send", json={"message": "hi"})

        threads = [threading.Thread(target=turn, args=(guid,)) for guid in chosen]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        for guid, (_, model) in chosen.items():
            assert answers[guid].status_code == 200, answers[guid].text
            assert answers[guid].json()["content"] == f"answer from {model}"
        assert shared._model_id is None
        assert sorted(OllamaStandIn.answered) == ["model-a", "model-b"]


@pytest.mark.usefixtures("shared")
class TestModelList:
    def test_names_each_users_own_default(self, client: TestClient) -> None:
        ada, _ = _signed_in(client, ADA)
        grace, _ = _signed_in(client, GRACE)
        nobody, _ = _signed_in(client, "nobody@example.com")
        _prefer(ada, "model-b")
        _prefer(grace, "model-a")

        listed = {
            name: browser.get("/conversations/api/models").json()
            for name, browser in (("ada", ada), ("grace", grace), ("nobody", nobody))
        }

        assert {n: v["default_model"] for n, v in listed.items()} == {
            "ada": "model-b",
            "grace": "model-a",
            "nobody": None,
        }
        for body in listed.values():
            assert sorted(m["id"] for m in body["models"]) == OFFERED
            assert body["mandatory"] is False

    def test_a_mandatory_default_is_each_users_own(self, client: TestClient) -> None:
        root = _root(client)
        ada, _ = _signed_in(client, ADA)
        grace, _ = _signed_in(client, GRACE)
        _prefer(ada, "model-b")
        _prefer(grace, "model-a")
        saved = root.post("/settings/api/instance", json={"default_model.mode": "mandatory"})
        assert saved.status_code == 200, saved.text

        for browser, model in ((ada, "model-b"), (grace, "model-a")):
            body = browser.get("/conversations/api/models").json()
            assert body["mandatory"] is True
            assert body["default_model"] == model
            assert [m["id"] for m in body["models"]] == [model]

    def test_a_default_no_longer_offered_is_not_named(self, client: TestClient) -> None:
        root = _root(client)
        ada, _ = _signed_in(client, ADA)
        _prefer(ada, "model-b")
        saved = root.post("/settings/api/instance", json={"models.offered": ["model-a"]})
        assert saved.status_code == 200, saved.text

        body = ada.get("/conversations/api/models").json()

        assert body["default_model"] is None
        assert [m["id"] for m in body["models"]] == ["model-a"]


@pytest.mark.usefixtures("shared")
class TestGraphBuildModel:
    def test_each_build_uses_its_users_default_model(self, client: TestClient) -> None:
        from spark.knowledge import jobs

        app: Any = client.app
        ada, ada_guid = _signed_in(client, ADA)
        grace, grace_guid = _signed_in(client, GRACE)
        _prefer(ada, "model-b")
        _prefer(grace, "model-a")
        # A previous turn or setting left the shared manager on another model.
        app.state.llm_manager.set_model(GONE)

        queued: list[str] = []
        for browser, guid in ((ada, ada_guid), (grace, grace_guid)):
            cid = _call(client, conversations.create_conversation, "c", "model-a", guid)
            _call(client, msg_db.add_message, cid, "user", "Tea is a drink", 5, guid)
            started = browser.post("/knowledge/api/build", json={"scope": f"conv:{cid}"})
            assert started.status_code == 200, started.text
            # On a server the build is queued for the worker with the model it will use.
            job = _call(client, jobs.get, guid, started.json()["job_id"])
            queued.append(job.model_id)

        assert queued == ["model-b", "model-a"]
