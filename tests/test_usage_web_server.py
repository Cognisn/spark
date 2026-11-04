"""The usage view, its export and budgets in server mode: administrators only, and audited.

The real app on a PostgreSQL clone at head with warden's chain; synthetic users.
Guards under test:

* the view, the CSV export and both budget forms need ``spark.usage.admin``,
  which only Administrators hold (401 signed out, 403 for every other role);
* users are shown by email address, each with their own usage;
* a role budget applies to that role's holders and a per-user override wins,
  as each user's own ``/api/usage/me`` shows;
* viewing, exporting and changing a budget are each audited.
"""

from __future__ import annotations

import csv
import io
import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.audit import AuditQuery, list_events
from warden.audit.store import AuditRow

from spark.database import autonomous_actions, usage
from spark.identity import audit as spark_audit
from spark.identity.db import warden_database
from spark.identity.permissions import ADMINISTRATOR, AUDITOR, POWER_USER, USER
from tests.identity_screen_helpers import NAVIGATE, plain_client, server_db  # noqa: F401
from tests.identity_web_helpers import another_client, make_user, sign_in

pytestmark = pytest.mark.postgres

ADMIN = "admin@example.com"
ADA = "ada@example.com"
GRACE = "grace@example.com"
FETCH = {"Accept": "application/json"}


def _guid(client: TestClient) -> str:
    return str(client.get("/auth/session", headers=FETCH).json()["user_guid"])


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def _events(client: TestClient, action: str) -> list[AuditRow]:
    app: Any = client.app

    async def read() -> list[AuditRow]:
        recorder = spark_audit.current()
        if recorder is not None:
            await recorder.drain()
        page = await list_events(warden_database(app.state.database), AuditQuery(actions=(action,)))
        return page.rows

    rows: list[AuditRow] = client.portal.call(read)  # type: ignore[attr-defined]
    return rows


def _signed_in(client: TestClient, email: str, *roles: str) -> TestClient:
    browser = another_client(client)
    make_user(client, email, *roles)
    assert sign_in(browser, email).status_code == 303
    return browser


@pytest.fixture
def admin(plain_client: TestClient) -> TestClient:
    make_user(plain_client, ADMIN, ADMINISTRATOR)
    assert sign_in(plain_client, ADMIN).status_code == 303
    return plain_client


class TestAccess:
    @pytest.mark.parametrize("role", [USER, POWER_USER, AUDITOR])
    def test_every_other_role_is_refused(self, admin: TestClient, role: str) -> None:
        other = _signed_in(admin, ADA, role)
        guid = _guid(other)
        assert other.get("/admin/usage", headers=NAVIGATE).status_code == 403
        assert other.get("/admin/usage/export.csv").status_code == 403
        assert (
            other.post(
                "/admin/usage/budgets/roles", data={"role": USER, "monthly_tokens": "1"}
            ).status_code
            == 403
        )
        assert (
            other.post(
                "/admin/usage/budgets/users", data={"user_guid": guid, "monthly_tokens": ""}
            ).status_code
            == 403
        )
        assert _call(admin, usage.role_budgets) == {}

    def test_signed_out_is_refused(self, admin: TestClient) -> None:
        anonymous = TestClient(admin.app, base_url=str(admin.base_url))
        anonymous.portal = admin.portal  # type: ignore[attr-defined]
        assert anonymous.get("/admin/usage/export.csv").status_code == 401
        assert anonymous.get("/api/usage/me").status_code == 401

    def test_an_administrator_sees_every_users_usage_by_email(self, admin: TestClient) -> None:
        ada = _guid(_signed_in(admin, ADA))
        grace = _guid(_signed_in(admin, GRACE))
        _call(admin, usage.record_usage, 1, "m1", 10, 5, ada)
        _call(admin, usage.record_usage, 2, "m2", 3, 1, grace)
        page = admin.get("/admin/usage", headers=NAVIGATE)
        assert page.status_code == 200
        found = set(re.findall(r'data-user="([^"]*)" data-model="([^"]*)"', page.text))
        assert found == {(ADA, "m1"), (GRACE, "m2")}

    def test_the_export_names_users_by_email(self, admin: TestClient) -> None:
        ada = _guid(_signed_in(admin, ADA))
        _call(admin, usage.record_usage, 1, "m1", 10, 5, ada)
        rows = list(csv.reader(io.StringIO(admin.get("/admin/usage/export.csv").text)))
        assert rows[1][1:4] == [ADA, ada, "m1"]


class TestBudgets:
    def test_a_role_budget_applies_to_its_holders(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        power = _signed_in(admin, GRACE, POWER_USER)
        resp = admin.post(
            "/admin/usage/budgets/roles",
            data={"role": USER, "monthly_tokens": "100"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        _call(admin, usage.record_usage, None, "m", 85, 0, _guid(ada))
        _call(admin, usage.record_usage, None, "m", 85, 0, _guid(power))
        mine = ada.get("/api/usage/me").json()
        assert (mine["limit"], mine["state"]) == (100, "warning")
        assert power.get("/api/usage/me").json()["state"] == "unlimited"

    def test_an_unknown_role_is_refused(self, admin: TestClient) -> None:
        resp = admin.post(
            "/admin/usage/budgets/roles", data={"role": "nobody", "monthly_tokens": "100"}
        )
        assert resp.status_code == 400
        assert _call(admin, usage.role_budgets) == {}

    def test_an_override_wins_for_that_user_only(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        grace = _signed_in(admin, GRACE, USER)
        _call(admin, usage.set_role_budget, USER, 100)
        resp = admin.post(
            "/admin/usage/budgets/users",
            data={"user_guid": _guid(ada), "monthly_tokens": "unlimited"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        _call(admin, usage.record_usage, None, "m", 100, 0, _guid(ada))
        _call(admin, usage.record_usage, None, "m", 100, 0, _guid(grace))
        assert ada.get("/api/usage/me").json()["state"] == "unlimited"
        assert grace.get("/api/usage/me").json()["state"] == "exceeded"

    def test_run_now_is_budgeted_by_the_users_own_roles(
        self, admin: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Any] = []

        class Executor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                seen.append(kwargs.get("roles"))

            def execute(self, action_id: int) -> None:
                pass

        monkeypatch.setattr("spark.scheduler.executor.ActionExecutor", Executor)
        monkeypatch.setattr("spark.web.endpoints.actions._actions_enabled", lambda request: True)
        ada = _signed_in(admin, ADA, POWER_USER)
        aid = _call(admin, autonomous_actions.create_action, "ada-run", "p", "m", _guid(ada))
        resp = ada.post(f"/actions/api/{aid}/run-now")
        assert resp.status_code == 200, resp.text
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [set(roles) for roles in seen] == [{POWER_USER}]

    def test_an_override_for_an_unknown_user_is_not_found(self, admin: TestClient) -> None:
        resp = admin.post(
            "/admin/usage/budgets/users", data={"user_guid": "no-such-user", "monthly_tokens": "5"}
        )
        assert resp.status_code == 404
        assert _call(admin, usage.user_overrides_all_users) == {}


class TestAudit:
    def test_viewing_is_audited(self, admin: TestClient) -> None:
        admin.get("/admin/usage?month=2026-10", headers=NAVIGATE)
        [event] = _events(admin, spark_audit.USAGE_VIEWED)
        assert event.resource_id == "2026-10"

    def test_exporting_is_audited(self, admin: TestClient) -> None:
        admin.get("/admin/usage/export.csv?month=2026-10")
        events = [
            e for e in _events(admin, spark_audit.EXPORT_DOWNLOADED) if e.resource_type == "usage"
        ]
        assert [e.resource_id for e in events] == ["2026-10"]

    def test_a_budget_change_is_audited(self, admin: TestClient) -> None:
        admin.post("/admin/usage/budgets/roles", data={"role": USER, "monthly_tokens": "100"})
        [event] = _events(admin, spark_audit.USAGE_BUDGET_CHANGED)
        assert (event.resource_type, event.resource_id) == ("role", USER)
        assert event.details["limit"] == 100

    def test_a_refused_request_is_audited(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        _call(admin, usage.set_role_budget, USER, 10)
        _call(admin, usage.record_usage, None, "m", 10, 0, _guid(ada))
        from spark.usage import budgets

        sync_db = admin.app.state.sync_db  # type: ignore[attr-defined]
        with pytest.raises(budgets.BudgetExceeded):
            budgets.check(sync_db, _guid(ada), {USER})
        [event] = _events(admin, spark_audit.USAGE_REFUSED)
        assert event.outcome == "failure"


class TestMenu:
    def test_only_an_administrator_is_offered_the_usage_view(self, admin: TestClient) -> None:
        assert 'href="/admin/usage"' in admin.get("/account", headers=NAVIGATE).text
        ada = _signed_in(admin, ADA, POWER_USER)
        assert 'href="/admin/usage"' not in ada.get("/account", headers=NAVIGATE).text
