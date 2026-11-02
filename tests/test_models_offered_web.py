"""Server mode: a model is chosen only from the models offered (D38).

The real app runs on a PostgreSQL clone at head with synthetic users signed in
through warden (the fixtures of ``tests.test_user_scoping_web``). An
administrator sets ``models.offered`` through the instance settings; the LLM
manager reads it live, as ``spark.instance_settings.consumers.follow`` wires it.

* Every place a user chooses a model refuses one that is not offered (409):
  creating a conversation (its debate and panel agents included), changing a
  conversation's model, creating and editing an action, AI-creating an action,
  approving a sub-agent's model and the default model preference.
* An offered model is accepted.
* A conversation pinned to a model that has left the list keeps working, and its
  info and page carry a notice that the model is no longer offered.
* Choosing a model never changes the shared manager's model, so two users
  choosing at once never affect each other.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.database import autonomous_actions, conversations
from spark.identity.permissions import ADMINISTRATOR, POWER_USER
from spark.llm.manager import LLMManager
from spark.web import interactions
from tests.test_conversation_manager import StubLLMService
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
OFFERED = ["model-a", "model-b"]
GONE = "model-old"


class OllamaStandIn(StubLLMService):
    """A provider the conversation manager can clone (as it clones Ollama) for one model."""

    lock = threading.Lock()
    answered: list[str] = []

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        super().__init__()
        self._base_url = base_url

    def get_provider_name(self) -> str:
        return "Ollama"

    def list_available_models(self) -> list[dict]:
        return [
            {"id": m, "name": m, "provider": "Ollama", "supports_tools": True, "context_length": 8}
            for m in (*OFFERED, GONE)
        ]

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        with self.lock:
            self.answered.append(str(self._model_id))
        text = f"answer from {self._model_id}"
        return {
            "content": text,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": text}],
        }


@pytest.fixture
def shared(client: TestClient) -> Iterator[OllamaStandIn]:
    """The app's LLM manager and conversation manager over one shared provider, and
    models.offered set by an administrator."""
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver

    app: Any = client.app
    OllamaStandIn.answered = []
    provider = OllamaStandIn()
    llm = LLMManager()
    llm.register_provider(provider)  # type: ignore[arg-type]
    source = app.state.instance_settings

    def offered() -> list[str]:
        value = source.get("models.offered")
        return [str(v) for v in value] if isinstance(value, list) else []

    llm.set_offered(offered)
    app.state.llm_manager = llm
    app.state.conversation_manager = ConversationManager(
        app.state.sync_db, llm, ContextLimitResolver(None), user_guid="process-guid"
    )
    root, _ = _signed_in(client, ROOT, ADMINISTRATOR)
    saved = root.post("/settings/api/instance", json={"models.offered": OFFERED})
    assert saved.status_code == 200, saved.text
    assert llm.offered_models() == frozenset(OFFERED)
    yield provider


def _agents(model: str) -> dict[str, Any]:
    return {role: {"model_id": model} for role in ("pro", "con", "judge")}


def _panel(model: str) -> dict[str, Any]:
    return {
        "topic": "Tea",
        "moderator": {"model_id": "model-a"},
        "panellists": [{"name": "One", "model_id": "model-a"}, {"name": "Two", "model_id": model}],
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
        cid = _call(client, conversations.create_conversation, "Ada's", "model-a", guid)
        aid = _call(client, autonomous_actions.create_action, "ada-a", "p", "model-a", guid)
        approval = _pending_approval(client, guid)
        refused = [
            ada.post("/conversations/api/create", json={"name": "x", "model_id": GONE}),
            ada.post(
                "/conversations/api/create",
                json={
                    "model_id": "model-a",
                    "conversation_type": "debate",
                    "debate": {"topic": "Tea", "agents": _agents(GONE)},
                },
            ),
            ada.post(
                "/conversations/api/create",
                json={"model_id": "model-a", "conversation_type": "panel", "panel": _panel(GONE)},
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
        assert ada.get(f"/chat/{cid}/api/info").json()["model_id"] == "model-a"
        assert [(a["name"], a["model_id"]) for a in ada.get("/actions/api/list").json()] == [
            ("ada-a", "model-a")
        ]
        assert not client.app.state.agent_model_events[approval].is_set()  # type: ignore[attr-defined]

    def test_an_offered_model_is_accepted(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        created = ada.post("/conversations/api/create", json={"name": "x", "model_id": "model-b"})
        assert created.status_code == 200, created.text
        cid = created.json()["id"]
        debate = ada.post(
            "/conversations/api/create",
            json={
                "model_id": "model-a",
                "conversation_type": "debate",
                "debate": {"topic": "Tea", "agents": _agents("model-b")},
            },
        )
        panel = ada.post(
            "/conversations/api/create",
            json={"model_id": "model-a", "conversation_type": "panel", "panel": _panel("model-b")},
        )
        assert (debate.status_code, panel.status_code) == (200, 200), (debate.text, panel.text)
        assert ada.post(f"/conversations/api/{cid}/model", json={"model_id": "model-a"}).json() == {
            "status": "ok"
        }
        action = ada.post(
            "/actions/api/create", json={"name": "ada-b", "prompt": "p", "model_id": "model-b"}
        )
        assert action.status_code == 200, action.text
        aid = action.json()["id"]
        assert ada.put(f"/actions/api/{aid}", json={"model_id": "model-a"}).status_code == 200
        made = ada.post("/actions/api/ai-create", json={"message": "hi", "model_id": "model-b"})
        assert made.status_code == 200, made.text
        assert made.json()["response"] == "answer from model-b"
        approval = _pending_approval(client, guid)
        approved = ada.post(
            "/chat/agent/model-approve", json={"request_id": approval, "model_id": "model-b"}
        )
        assert approved.status_code == 200
        assert client.app.state.agent_model_responses[approval] == "model-b"  # type: ignore[attr-defined]
        pref = ada.post("/settings/api/preferences", json={"default_model.model_id": "model-b"})
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
        current = _call(client, conversations.create_conversation, "New", "model-a", guid)

        sent = ada.post(f"/chat/{pinned}/api/send", json={"message": "hello"})
        assert sent.status_code == 200, sent.text
        assert sent.json()["content"] == f"answer from {GONE}"

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
    def test_concurrent_choices_never_touch_the_shared_model(
        self, client: TestClient, shared: OllamaStandIn
    ) -> None:
        ada, ada_guid = _signed_in(client, ADA, POWER_USER)
        grace, grace_guid = _signed_in(client, GRACE, POWER_USER)
        chosen = {ada_guid: "model-a", grace_guid: "model-b"}
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
                assert answer == f"answer from {model}"
                row = _call(client, conversations.get_conversation, cid, guid)
                assert row["model_id"] == model
        # Each choice used its own service; the shared provider's model never moved.
        assert shared._model_id is None
        assert sorted(set(OllamaStandIn.answered)) == ["model-a", "model-b"]
