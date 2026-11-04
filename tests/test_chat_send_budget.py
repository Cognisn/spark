"""The plain send route and the budget: roles passed down, a refusal answered 429.

``POST /chat/{id}/api/send`` is the non-streaming fallback for a chat turn. It
must be budgeted exactly as ``/stream/chat`` is: the turn is given the signed-in
user's roles, so a role budget stops it, and a refusal is answered 429 with the
user's standing, never 500, and without calling the model.

Server-mode tests run the real app on a PostgreSQL clone at head with warden's
chain; synthetic users. The personal-mode tests use SQLite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations as convdb
from spark.database import messages, usage
from spark.identity.permissions import ADMINISTRATOR, USER
from spark.usage.budgets import BudgetExceeded, BudgetStatus
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.identity_screen_helpers import plain_client, server_db  # noqa: F401
from tests.identity_web_helpers import another_client, make_user, sign_in
from tests.test_conversation_manager_budgets import ScriptedLLM, _manager
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in as sign_in_personal

ADMIN = "admin@example.com"
ADA = "ada@example.com"
GRACE = "grace@example.com"
FETCH = {"Accept": "application/json"}
LOCAL = "u1"


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def _guid(client: TestClient) -> str:
    return str(client.get("/auth/session", headers=FETCH).json()["user_guid"])


def _install_manager(client: TestClient) -> ScriptedLLM:
    """A real conversation manager on the app's database whose model is a counting stub."""
    mgr, stub = _manager(client.app.state.sync_db)  # type: ignore[attr-defined]
    mgr._get_vector_index = lambda conversation_id, user_guid: None
    client.app.state.conversation_manager = mgr  # type: ignore[attr-defined]
    stub_llm: ScriptedLLM = stub
    return stub_llm


def _conversation(client: TestClient, guid: str) -> int:
    cid: int = _call(client, convdb.create_conversation, "Synthetic", "stub-model", guid)
    return cid


def _send(client: TestClient, cid: int, text: str = "hello") -> Any:
    return client.post(f"/chat/{cid}/api/send", json={"message": text})


def _assert_refused(resp: Any) -> None:
    assert resp.status_code == 429, (resp.status_code, resp.text[:500])
    body = resp.json()
    assert "monthly token budget" in body["error"]
    assert body["budget"]["state"] == "exceeded"


# -- Server mode (PostgreSQL) ------------------------------------------------------------


@pytest.fixture
def admin(plain_client: TestClient) -> TestClient:  # noqa: F811
    make_user(plain_client, ADMIN, ADMINISTRATOR)
    assert sign_in(plain_client, ADMIN).status_code == 303
    return plain_client


def _signed_in(client: TestClient, email: str, *roles: str) -> TestClient:
    browser = another_client(client)
    make_user(client, email, *roles)
    assert sign_in(browser, email).status_code == 303
    return browser


@pytest.mark.postgres
class TestServerSendRoute:
    def test_a_role_budget_already_exceeded_refuses_with_429_without_the_model(
        self, admin: TestClient
    ) -> None:
        stub = _install_manager(admin)
        ada = _signed_in(admin, ADA, USER)
        guid = _guid(ada)
        cid = _conversation(admin, guid)
        _call(admin, usage.set_role_budget, USER, 10)
        _call(admin, usage.record_usage, None, "m", 10, 0, guid)
        assert ada.get("/api/usage/me").json()["state"] == "exceeded"

        resp = _send(ada, cid)

        _assert_refused(resp)
        assert stub.calls == 0
        assert _call(admin, messages.get_messages, guid, cid) == []

    def test_a_per_user_override_refuses_the_same_way(self, admin: TestClient) -> None:
        stub = _install_manager(admin)
        ada = _signed_in(admin, ADA, USER)
        guid = _guid(ada)
        cid = _conversation(admin, guid)
        _call(admin, usage.set_user_override, guid, 10)
        _call(admin, usage.record_usage, None, "m", 10, 0, guid)

        resp = _send(ada, cid)

        _assert_refused(resp)
        assert stub.calls == 0
        assert _call(admin, messages.get_messages, guid, cid) == []

    def test_a_user_within_budget_is_answered_as_before(self, admin: TestClient) -> None:
        stub = _install_manager(admin)
        ada = _signed_in(admin, ADA, USER)
        grace = _signed_in(admin, GRACE, USER)
        guid = _guid(ada)
        cid = _conversation(admin, guid)
        _call(admin, usage.set_role_budget, USER, 1000)
        # Another holder of the role at the limit does not stop ada.
        _call(admin, usage.record_usage, None, "m", 1000, 0, _guid(grace))

        resp = _send(ada, cid)

        assert resp.status_code == 200, resp.text[:500]
        assert resp.json()["content"] == "done"
        assert stub.calls == 1
        assert resp.json()["budget"]["limit"] == 1000


# -- Personal mode (SQLite) --------------------------------------------------------------


@pytest.fixture()
def personal(sqlite_settings: Any) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = LOCAL
        sign_in_personal(c)
        yield c


class TestPersonalSendRoute:
    def test_an_override_refuses_with_429_without_the_model(self, personal: TestClient) -> None:
        stub = _install_manager(personal)
        cid = _conversation(personal, LOCAL)
        _call(personal, usage.set_user_override, LOCAL, 10)
        _call(personal, usage.record_usage, None, "m", 10, 0, LOCAL)

        resp = _send(personal, cid)

        _assert_refused(resp)
        assert stub.calls == 0
        assert _call(personal, messages.get_messages, LOCAL, cid) == []

    def test_the_turn_is_given_the_users_roles(
        self, personal: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_manager(personal)
        cid = _conversation(personal, LOCAL)
        mgr = personal.app.state.conversation_manager  # type: ignore[attr-defined]
        seen: dict[str, Any] = {}

        def send(*args: Any, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"content": "x", "status": "completed"}

        monkeypatch.setattr(mgr, "send_message", send)
        assert _send(personal, cid).status_code == 200
        assert seen["roles"] == frozenset({ADMINISTRATOR})

    def test_another_failure_is_still_500_and_a_refusal_never_is(
        self, personal: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_manager(personal)
        cid = _conversation(personal, LOCAL)
        mgr = personal.app.state.conversation_manager  # type: ignore[attr-defined]

        def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(mgr, "send_message", broken)
        assert _send(personal, cid).status_code == 500

        def refusing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise BudgetExceeded(BudgetStatus("2026-11", used=10, limit=10))

        monkeypatch.setattr(mgr, "send_message", refusing)
        resp = _send(personal, cid)
        _assert_refused(resp)
        assert resp.json()["budget"]["period"] == "2026-11"
