"""The turn endpoints and the budget: roles passed down, refusals sent clearly (personal mode).

A refused chat turn streams a ``budget_exceeded`` event with the message and
stores nothing; a finished turn's final response carries the user's standing;
debate and panel streams relay a refusal the same way, and a question in QA is
answered 429 without being stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import debates, messages, usage
from spark.identity.permissions import ADMINISTRATOR
from spark.usage.budgets import BudgetExceeded, BudgetStatus
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_conversation_manager_budgets import _manager
from tests.test_debate_endpoints import DEBATE_PAYLOAD
from tests.test_panel_endpoints import PANEL_PAYLOAD
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in

LOCAL = "u1"


@pytest.fixture()
def client(sqlite_settings: Any) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = LOCAL
        mgr, _stub = _manager(app.state.sync_db)
        app.state.conversation_manager = mgr
        sign_in(c)
        yield c


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def _chat(client: TestClient) -> int:
    mgr = client.app.state.conversation_manager  # type: ignore[attr-defined]
    cid: int = mgr.create_conversation("chat", "stub-model", LOCAL)
    return cid


class TestChatStream:
    def test_a_refused_turn_streams_the_refusal_and_stores_nothing(
        self, client: TestClient
    ) -> None:
        cid = _chat(client)
        _call(client, usage.set_user_override, LOCAL, 10)
        _call(client, usage.record_usage, None, "m", 10, 0, LOCAL)
        resp = client.get(f"/stream/chat?message=hello&conversation_id={cid}")
        assert resp.status_code == 200
        assert "event: budget_exceeded" in resp.text
        assert "monthly token budget" in resp.text
        assert "event: error" not in resp.text
        assert _call(client, messages.get_messages, LOCAL, cid) == []

    def test_the_final_response_carries_the_standing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The manager's own result is tested with it; here only the endpoint's relay.
        cid = _chat(client)
        mgr = client.app.state.conversation_manager  # type: ignore[attr-defined]
        standing = BudgetStatus("2026-11", used=50, limit=60).as_dict()
        monkeypatch.setattr(
            mgr,
            "send_message",
            lambda *a, **k: {"content": "x", "status": "completed", "budget": standing},
        )
        resp = client.get(f"/stream/chat?message=hello&conversation_id={cid}")
        assert '"budget": {"period": "2026-11"' in resp.text
        assert '"state": "warning"' in resp.text

    def test_the_turn_is_given_the_users_roles(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cid = _chat(client)
        mgr = client.app.state.conversation_manager  # type: ignore[attr-defined]
        seen: dict[str, Any] = {}

        def send(*args: Any, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"content": "x", "status": "completed"}

        monkeypatch.setattr(mgr, "send_message", send)
        client.get(f"/stream/chat?message=hello&conversation_id={cid}")
        assert seen["roles"] == frozenset({ADMINISTRATOR})


class _RefusingOrchestrator:
    def __init__(self) -> None:
        self.roles: Any = "never set"

    def run(self, conversation_id: int, user_guid: str, **kwargs: Any) -> None:
        self.roles = kwargs.get("roles")
        raise BudgetExceeded(BudgetStatus("2026-11", used=10, limit=10))


@pytest.mark.parametrize("kind", ["debate", "panel"])
def test_a_refused_debate_or_panel_run_streams_the_refusal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import importlib

    module = importlib.import_module(f"spark.web.endpoints.{kind}")
    fake = _RefusingOrchestrator()
    monkeypatch.setattr(module, "_orchestrator", lambda request, **kwargs: fake)
    payload = PANEL_PAYLOAD if kind == "panel" else DEBATE_PAYLOAD
    created = client.post("/conversations/api/create", json=payload)
    assert created.status_code == 200, created.text
    cid = created.json()["id"]
    resp = client.get(f"/stream/{kind}?conversation_id={cid}")
    assert "event: budget_exceeded" in resp.text
    assert "monthly token budget" in resp.text
    assert fake.roles == frozenset({ADMINISTRATOR})


class TestQaQuestion:
    def test_refused_with_429_and_not_stored(self, client: TestClient) -> None:
        cid = client.post("/conversations/api/create", json=DEBATE_PAYLOAD).json()["id"]
        _call(client, debates.update_debate_state, LOCAL, cid, "qa")
        _call(client, usage.set_user_override, LOCAL, 10)
        _call(client, usage.record_usage, None, "m", 10, 0, LOCAL)
        before = _call(client, debates.get_turns, LOCAL, cid)
        resp = client.post("/debate/api/prompt", json={"conversation_id": cid, "message": "Why?"})
        assert resp.status_code == 429
        assert "monthly token budget" in resp.json()["error"]
        assert resp.json()["budget"]["state"] == "exceeded"
        assert _call(client, debates.get_turns, LOCAL, cid) == before


class TestPanelQaQuestion:
    def test_refused_with_429_and_not_stored(self, client: TestClient) -> None:
        cid = client.post("/conversations/api/create", json=PANEL_PAYLOAD).json()["id"]
        _call(client, debates.update_debate_state, LOCAL, cid, "qa")
        _call(client, usage.set_user_override, LOCAL, 10)
        _call(client, usage.record_usage, None, "m", 10, 0, LOCAL)
        before = _call(client, debates.get_turns, LOCAL, cid)
        resp = client.post("/panel/api/prompt", json={"conversation_id": cid, "message": "Why?"})
        assert resp.status_code == 429
        assert resp.json()["budget"]["state"] == "exceeded"
        assert _call(client, debates.get_turns, LOCAL, cid) == before
