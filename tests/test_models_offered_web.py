"""Server mode: a model is chosen only from the models the user's connections offer (M5, M9).

The real app runs on a PostgreSQL clone at head with synthetic users signed in
through warden (the fixtures of ``tests.test_user_scoping_web``), over a real
connection runtime on conduit's stub provider: one connection, assigned to every
role, offering ``model-a`` and ``model-b``, with ``model-old`` declared in the
catalogue but no longer enabled.

* Every place a user chooses a model refuses one that is not offered (409):
  creating a conversation (its debate and panel agents included), changing a
  conversation's model, creating and editing an action, AI-creating an action,
  approving a sub-agent's model and the default model preference.
* An offered model is accepted.
* A conversation pinned to a model no longer offered keeps working, and its info
  and page carry a notice that the model is no longer offered.
* Choosing a model changes nothing shared, so two users choosing at once never
  affect each other.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from conduit import ModelRequest
from conduit.providers.stub import StubTurn
from fastapi.testclient import TestClient

from spark.connections import ConnectionModel, ConnectionRuntime
from spark.database import autonomous_actions, conversations
from spark.database.connections import Assignment, OverlayRow
from spark.identity.permissions import ADMINISTRATOR, POWER_USER
from spark.web import interactions
from tests.model_helpers import stub_connection, stub_runtime
from tests.test_user_scoping_web import (  # noqa: F401 - fixtures
    _call,
    _signed_in,
    client,
    host,
    server_db,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
ROOT = "root@example.com"
MODEL_A = "stub/model-a"
MODEL_B = "stub/model-b"
OFFERED = [MODEL_A, MODEL_B]
GONE = "stub/model-old"
ROLES = ("user", "power_user", "auditor", "administrator")


class Answers:
    """The stub's script: answers with the model it was called on, and remembers it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.answered: list[str] = []

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        with self.lock:
            self.answered.append(request.model)
        return StubTurn(text=f"answer from {request.model}")


def offered_runtime(sync_db: Any, answers: Answers) -> ConnectionRuntime:
    """The connection runtime these web tests share (see the module docstring)."""
    return stub_runtime(
        sync_db,
        connections=[stub_connection("stub", script="answers")],
        scripts={"answers": answers},
        assignments=[Assignment("stub", role=role) for role in ROLES],
        overlays=[
            OverlayRow(
                "stub",
                model,
                200_000,
                "local",
                output_limit=8_192,
                tools=True,
                connection_id="stub",
            )
            for model in ("model-a", "model-b", "model-old")
        ],
        models=[
            ConnectionModel("stub", "model-a"),
            ConnectionModel("stub", "model-b"),
            ConnectionModel("stub", "model-old", enabled=False),
        ],
    )


@pytest.fixture
def shared(client: TestClient) -> Iterator[Answers]:
    """The app's connection runtime and conversation manager over one stub connection."""
    from spark.core.conversation_manager import ConversationManager

    app: Any = client.app
    answers = Answers()
    runtime = offered_runtime(app.state.sync_db, answers)
    app.state.connections = runtime  # closed with the app
    app.state.conversation_manager = ConversationManager(
        app.state.sync_db, runtime, user_guid="process-guid"
    )
    _signed_in(client, ROOT, ADMINISTRATOR)
    yield answers


def _agents(model: str) -> dict[str, Any]:
    return {role: {"model_id": model} for role in ("pro", "con", "judge")}


def _panel(model: str) -> dict[str, Any]:
    return {
        "topic": "Tea",
        "moderator": {"model_id": MODEL_A},
        "panellists": [{"name": "One", "model_id": MODEL_A}, {"name": "Two", "model_id": model}],
    }


def _pending_approval(client: TestClient, guid: str) -> str:
    """A sub-agent model approval waiting for ``guid``'s answer, as a stream registers one."""
    state: Any = client.app.state
    state.agent_model_events = {"approve-1": threading.Event()}
    state.agent_model_responses = {"approve-1": None}
    interactions.bind(client.app, interactions.AGENT_MODEL, "approve-1", guid)
    return "approve-1"


@pytest.mark.usefixtures("shared")
class TestSelectionPoints:
    def test_a_model_not_offered_is_refused_everywhere(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        cid = _call(client, conversations.create_conversation, "Ada's", MODEL_A, guid)
        aid = _call(client, autonomous_actions.create_action, "ada-a", "p", MODEL_A, guid)
        approval = _pending_approval(client, guid)
        refused = [
            ada.post("/conversations/api/create", json={"name": "x", "model_id": GONE}),
            ada.post(
                "/conversations/api/create",
                json={
                    "model_id": MODEL_A,
                    "conversation_type": "debate",
                    "debate": {"topic": "Tea", "agents": _agents(GONE)},
                },
            ),
            ada.post(
                "/conversations/api/create",
                json={"model_id": MODEL_A, "conversation_type": "panel", "panel": _panel(GONE)},
            ),
            ada.post(f"/conversations/api/{cid}/model", json={"model_id": GONE}),
            ada.post(
                "/actions/api/create", json={"name": "ada-b", "prompt": "p", "model_id": GONE}
            ),
            ada.put(f"/actions/api/{aid}", json={"model_id": GONE}),
            ada.post("/actions/api/ai-create", json={"message": "make one", "model_id": GONE}),
            ada.post("/chat/agent/model-approve", json={"request_id": approval, "model_id": GONE}),
            ada.post("/settings/api/preferences", json={"default_model.model_id": GONE}),
        ]
        assert [r.status_code for r in refused] == [409] * len(refused), [
            (r.request.method, r.request.url.path, r.status_code, r.text) for r in refused
        ]
        assert all(GONE in r.text for r in refused)
        # Nothing was created or changed.
        assert [c["id"] for c in ada.get("/conversations/api/list").json()] == [cid]
        assert ada.get(f"/chat/{cid}/api/info").json()["model_id"] == MODEL_A
        assert [(a["name"], a["model_id"]) for a in ada.get("/actions/api/list").json()] == [
            ("ada-a", MODEL_A)
        ]
        assert not client.app.state.agent_model_events[approval].is_set()  # type: ignore[attr-defined]

    def test_an_offered_model_is_accepted(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        created = ada.post("/conversations/api/create", json={"name": "x", "model_id": MODEL_B})
        assert created.status_code == 200, created.text
        cid = created.json()["id"]
        debate = ada.post(
            "/conversations/api/create",
            json={
                "model_id": MODEL_A,
                "conversation_type": "debate",
                "debate": {"topic": "Tea", "agents": _agents(MODEL_B)},
            },
        )
        panel = ada.post(
            "/conversations/api/create",
            json={"model_id": MODEL_A, "conversation_type": "panel", "panel": _panel(MODEL_B)},
        )
        assert (debate.status_code, panel.status_code) == (200, 200), (debate.text, panel.text)
        assert ada.post(f"/conversations/api/{cid}/model", json={"model_id": MODEL_A}).json() == {
            "status": "ok"
        }
        action = ada.post(
            "/actions/api/create", json={"name": "ada-b", "prompt": "p", "model_id": MODEL_B}
        )
        assert action.status_code == 200, action.text
        aid = action.json()["id"]
        assert ada.put(f"/actions/api/{aid}", json={"model_id": MODEL_A}).status_code == 200
        made = ada.post("/actions/api/ai-create", json={"message": "hi", "model_id": MODEL_B})
        assert made.status_code == 200, made.text
        assert made.json()["response"] == "answer from model-b"
        approval = _pending_approval(client, guid)
        approved = ada.post(
            "/chat/agent/model-approve", json={"request_id": approval, "model_id": MODEL_B}
        )
        assert approved.status_code == 200
        assert client.app.state.agent_model_responses[approval] == MODEL_B  # type: ignore[attr-defined]
        pref = ada.post("/settings/api/preferences", json={"default_model.model_id": MODEL_B})
        assert pref.status_code == 200, pref.text


@pytest.mark.usefixtures("shared")
class TestPinnedConversation:
    def test_keeps_working_and_carries_a_notice(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA)
        pinned = _call(client, conversations.create_conversation, "Old", GONE, guid)
        # A plain turn: no memory or retrieval (no embedding model needed).
        _call(
            client,
            conversations.update_conversation,
            pinned,
            guid,
            memory_enabled=0,
            rag_enabled=0,
        )
        current = _call(client, conversations.create_conversation, "New", MODEL_A, guid)

        sent = ada.post(f"/chat/{pinned}/api/send", json={"message": "hello"})
        assert sent.status_code == 200, sent.text
        assert sent.json()["content"] == "answer from model-old"

        info = ada.get(f"/chat/{pinned}/api/info").json()
        assert info["model_id"] == GONE
        assert info["model_offered"] is False
        assert GONE in info["model_notice"] and "no longer offered" in info["model_notice"]
        page = ada.get(f"/chat/{pinned}")
        assert 'id="model-notice"' in page.text
        assert "no longer offered" in page.text

        fine = ada.get(f"/chat/{current}/api/info").json()
        assert (fine["model_offered"], fine["model_notice"]) == (True, None)
        assert 'id="model-notice"' not in ada.get(f"/chat/{current}").text


@pytest.mark.usefixtures("shared")
class TestUsersDoNotAffectEachOther:
    def test_concurrent_choices_each_use_their_own_model(
        self, client: TestClient, shared: Answers
    ) -> None:
        ada, ada_guid = _signed_in(client, ADA, POWER_USER)
        grace, grace_guid = _signed_in(client, GRACE, POWER_USER)
        chosen = {ada_guid: MODEL_A, grace_guid: MODEL_B}
        results: dict[str, list[Any]] = {ada_guid: [], grace_guid: []}
        barrier = threading.Barrier(2, timeout=30)

        def choose(browser: TestClient, guid: str) -> None:
            model = chosen[guid]
            barrier.wait()
            for i in range(3):
                made = browser.post(
                    "/conversations/api/create", json={"name": f"c{i}", "model_id": model}
                )
                cid = made.json()["id"]
                browser.post(f"/conversations/api/{cid}/model", json={"model_id": model})
                answer = browser.post(
                    "/actions/api/ai-create", json={"message": "hi", "model_id": model}
                )
                results[guid].append((cid, answer.json()["response"]))

        threads = [
            threading.Thread(target=choose, args=(ada, ada_guid)),
            threading.Thread(target=choose, args=(grace, grace_guid)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        for guid, model in chosen.items():
            assert len(results[guid]) == 3
            for cid, answer in results[guid]:
                assert answer == f"answer from {model.split('/', 1)[1]}"
                row = _call(client, conversations.get_conversation, cid, guid)
                assert row["model_id"] == model
        assert sorted(set(shared.answered)) == ["model-a", "model-b"]


@pytest.mark.usefixtures("shared")
class TestAssignment:
    """A connection reaches only the users and roles assigned it (M9)."""

    def test_an_unassigned_connection_is_not_offered(self, client: TestClient) -> None:
        app: Any = client.app
        runtime = app.state.connections
        snapshot_connections = [stub_connection("stub", script="answers")]
        from spark.connections.runtime import snapshot_of

        # Only the administrator role keeps the connection.
        runtime.apply(
            snapshot_of(
                snapshot_connections,
                [ConnectionModel("stub", "model-a")],
                [Assignment("stub", role="administrator")],
                [
                    OverlayRow(
                        "stub",
                        "model-a",
                        200_000,
                        "local",
                        output_limit=8_192,
                        connection_id="stub",
                    )
                ],
            )
        )
        ada, guid = _signed_in(client, ADA, POWER_USER)
        assert ada.get("/conversations/api/models").json()["models"] == []
        refused = ada.post("/conversations/api/create", json={"name": "x", "model_id": MODEL_A})
        assert refused.status_code == 409
        # A conversation already on it is refused at the turn, not answered.
        cid = _call(client, conversations.create_conversation, "Old", MODEL_A, guid)
        _call(client, conversations.update_conversation, cid, guid, memory_enabled=0, rag_enabled=0)
        sent = ada.post(f"/chat/{cid}/api/send", json={"message": "hello"})
        assert sent.status_code == 403 and "not available to you" in sent.text
