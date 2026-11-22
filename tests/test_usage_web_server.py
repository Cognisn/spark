"""The usage view, its export and credit allowances in server mode: administrators only.

The real app on a PostgreSQL clone at head with warden's chain; synthetic users.
Guards under test:

* the view and the CSV export need ``spark.usage.admin``, which only
  Administrators hold (401 signed out, 403 for every other role);
* users are shown by email address, each with their own usage;
* a role's allowance on a metered connection applies to that role's holders, a
  user's own assignment wins, and the highest of several roles applies, as each
  user's own ``/api/usage/me`` shows; an unmetered connection has no limit;
* viewing and exporting are audited, and so is a refused request.
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
from tests.credit_helpers import allow, metered, spent
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
        # Their own standing is theirs to read.
        assert other.get("/api/usage/me").status_code == 200
        assert guid

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
        found = set(
            re.findall(
                r'data-user="([^"]*)" data-connection="[^"]*" data-model="([^"]*)"', page.text
            )
        )
        assert found == {(ADA, "m1"), (GRACE, "m2")}

    def test_the_export_names_users_by_email(self, admin: TestClient) -> None:
        ada = _guid(_signed_in(admin, ADA))
        _call(admin, usage.record_usage, 1, "m1", 10, 5, ada)
        rows = list(csv.reader(io.StringIO(admin.get("/admin/usage/export.csv").text)))
        assert rows[1][1:3] == [ADA, ada]
        assert rows[1][5] == "m1"


class TestAllowances:
    def test_a_role_allowance_applies_to_its_holders(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        power = _signed_in(admin, GRACE, POWER_USER)
        _call(admin, metered, name="Work Bedrock")
        _call(admin, allow, role=USER, credits=100)
        _call(admin, allow, role=POWER_USER, credits=None)
        _call(admin, spent, _guid(ada), 85)
        _call(admin, spent, _guid(power), 85)
        mine = ada.get("/api/usage/me").json()
        assert (mine["limit"], mine["state"], mine["connection"]) == (
            100,
            "warning",
            "Work Bedrock",
        )
        assert mine["connections"]["stub"]["remaining"] == 15
        assert power.get("/api/usage/me?connection=stub").json()["state"] == "unlimited"

    def test_the_users_own_assignment_wins_for_that_user_only(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        grace = _signed_in(admin, GRACE, USER)
        _call(admin, metered)
        _call(admin, allow, role=USER, credits=100)
        _call(admin, allow, user=_guid(ada), credits=None)
        _call(admin, spent, _guid(ada), 100)
        _call(admin, spent, _guid(grace), 100)
        assert ada.get("/api/usage/me?connection=stub").json()["state"] == "unlimited"
        assert grace.get("/api/usage/me?connection=stub").json()["state"] == "exceeded"

    def test_the_highest_of_several_roles(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER, AUDITOR)
        _call(admin, metered)
        _call(admin, allow, role=USER, credits=100)
        _call(admin, allow, role=AUDITOR, credits=300)
        _call(admin, spent, _guid(ada), 150)
        mine = ada.get("/api/usage/me?model=stub/any-model").json()
        assert (mine["limit"], mine["state"]) == (300, "ok")

    def test_an_unmetered_connection_is_not_listed(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        _call(admin, metered, is_metered=False)
        _call(admin, allow, role=USER)
        _call(admin, spent, _guid(ada), 10**6)
        mine = ada.get("/api/usage/me").json()
        assert (mine["state"], mine["connections"]) == ("unlimited", {})

    def test_the_view_shows_each_users_standing_per_connection(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        _call(admin, metered, name="Work Bedrock")
        _call(admin, allow, role=USER, credits=100)
        _call(admin, spent, _guid(ada), 90)
        page = admin.get("/admin/usage", headers=NAVIGATE)
        assert re.search(
            rf'class="standing-row" data-user="{re.escape(ADA)}" data-connection="stub"'
            r' data-standing="warning"',
            page.text,
        )

    def test_run_now_is_metered_by_the_users_own_roles(
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

    def test_a_refused_request_is_audited_with_its_connection(self, admin: TestClient) -> None:
        ada = _signed_in(admin, ADA, USER)
        _call(admin, metered)
        _call(admin, allow, role=USER, credits=10)
        _call(admin, spent, _guid(ada), 10)
        from spark.usage import budgets

        sync_db = admin.app.state.sync_db  # type: ignore[attr-defined]
        with pytest.raises(budgets.BudgetExceeded):
            budgets.check(sync_db, _guid(ada), {USER}, connection_id="stub")
        [event] = _events(admin, spark_audit.USAGE_REFUSED)
        assert event.outcome == "failure"
        assert event.details["connection_id"] == "stub"


class TestMenu:
    def test_only_an_administrator_is_offered_the_usage_view(self, admin: TestClient) -> None:
        # The usage view is in the Administration area, which a power user does not have.
        assert 'href="/admin/usage"' in admin.get("/admin", headers=NAVIGATE).text
        ada = _signed_in(admin, ADA, POWER_USER)
        assert 'href="/admin/usage"' not in ada.get("/account", headers=NAVIGATE).text
        assert ada.get("/admin", headers=NAVIGATE).status_code == 403
