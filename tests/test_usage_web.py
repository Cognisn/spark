"""The usage view, its export, budgets and a user's own standing on the web: personal mode.

Personal mode's single local user holds every permission, so the view is
theirs; role budgets do not apply there, and only the local user's own budget
can be set. Server mode's guards are in ``test_usage_web_server.py``.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import usage
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in

LOCAL = "u1"
NOW = datetime.now(timezone.utc)
PERIOD = usage.period_of(NOW)
OCTOBER = datetime(2026, 10, 15, tzinfo=timezone.utc)
NAVIGATE = {"Accept": "text/html"}


@pytest.fixture()
def client(sqlite_settings: Any) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = LOCAL
        sign_in(c)
        yield c


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def _rows(page: Any) -> list[tuple[str, str, str, int]]:
    return [
        (m[0], m[1], m[2], int(m[3]))
        for m in re.findall(
            r'<tr class="usage-row" data-period="([^"]*)" data-user="([^"]*)"'
            r' data-model="([^"]*)" data-total="(\d+)"',
            page.text,
        )
    ]


class TestMyUsage:
    def test_unlimited_without_a_budget(self, client: TestClient) -> None:
        _call(client, usage.record_usage, None, "m1", 10**6, 0, LOCAL)
        data = client.get("/api/usage/me").json()
        assert (data["state"], data["used"], data["limit"]) == ("unlimited", 10**6, None)

    def test_the_80_percent_warning(self, client: TestClient) -> None:
        _call(client, usage.set_user_override, LOCAL, 100)
        _call(client, usage.record_usage, None, "m1", 80, 5, LOCAL)
        data = client.get("/api/usage/me").json()
        assert (data["state"], data["percent"], data["period"]) == ("warning", 85.0, PERIOD)

    def test_exceeded(self, client: TestClient) -> None:
        _call(client, usage.set_user_override, LOCAL, 100)
        _call(client, usage.record_usage, None, "m1", 100, 0, LOCAL)
        assert client.get("/api/usage/me").json()["state"] == "exceeded"


class TestView:
    def test_usage_by_month_user_and_model(self, client: TestClient) -> None:
        _call(client, usage.record_usage, 1, "m1", 10, 5, LOCAL)
        _call(client, usage.record_usage, 1, "m1", 20, 5, LOCAL)
        _call(client, usage.record_usage, 1, "m2", 7, 0, LOCAL)
        _call(client, usage.record_usage, 1, "m1", 99, 0, LOCAL, at=OCTOBER)
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert page.status_code == 200
        assert _rows(page) == [
            (PERIOD, "Local user", "m1", 40),
            (PERIOD, "Local user", "m2", 7),
        ]
        october = client.get("/admin/usage?month=2026-10", headers=NAVIGATE)
        assert _rows(october) == [("2026-10", "Local user", "m1", 99)]
        every = client.get("/admin/usage?month=all", headers=NAVIGATE)
        assert len(_rows(every)) == 3

    def test_a_bad_month_is_explained(self, client: TestClient) -> None:
        page = client.get("/admin/usage?month=2026-13", headers=NAVIGATE)
        assert page.status_code == 400
        assert "month" in page.text.lower()

    def test_the_view_shows_the_local_users_standing(self, client: TestClient) -> None:
        _call(client, usage.set_user_override, LOCAL, 1000)
        _call(client, usage.record_usage, 1, "m1", 900, 0, LOCAL)
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert re.search(r'data-standing="warning"', page.text)


class TestExport:
    def test_csv_of_one_month(self, client: TestClient) -> None:
        _call(client, usage.record_usage, 1, "m1", 10, 5, LOCAL)
        _call(client, usage.record_usage, 1, "m1", 99, 0, LOCAL, at=OCTOBER)
        resp = client.get(f"/admin/usage/export.csv?month={PERIOD}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.headers["cache-control"] == "no-store"
        rows = list(csv.reader(io.StringIO(resp.text)))
        assert rows[0] == [
            "month",
            "user",
            "user_guid",
            "model",
            "requests",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ]
        assert rows[1:] == [[PERIOD, "Local user", LOCAL, "m1", "1", "10", "5", "15"]]

    def test_every_month(self, client: TestClient) -> None:
        _call(client, usage.record_usage, 1, "m1", 10, 5, LOCAL)
        _call(client, usage.record_usage, 1, "m1", 99, 0, LOCAL, at=OCTOBER)
        rows = list(csv.reader(io.StringIO(client.get("/admin/usage/export.csv").text)))
        assert [r[0] for r in rows[1:]] == [PERIOD, "2026-10"]

    def test_cells_that_look_like_formulas_are_neutralised(self, client: TestClient) -> None:
        _call(client, usage.record_usage, 1, '=HYPERLINK("http://example.com")', 1, 1, LOCAL)
        rows = list(csv.reader(io.StringIO(client.get("/admin/usage/export.csv").text)))
        assert rows[1][3].startswith("'=")


class TestOwnBudget:
    def _set(self, client: TestClient, value: str, guid: str = LOCAL) -> Any:
        return client.post(
            "/admin/usage/budgets/users",
            data={"user_guid": guid, "monthly_tokens": value},
            follow_redirects=False,
        )

    def test_set_unlimited_and_clear(self, client: TestClient) -> None:
        assert self._set(client, "5000").status_code == 303
        assert _call(client, usage.user_override, LOCAL) == usage.UserOverride(5000)
        assert self._set(client, "unlimited").status_code == 303
        assert _call(client, usage.user_override, LOCAL) == usage.UserOverride(None)
        assert self._set(client, "").status_code == 303
        assert _call(client, usage.user_override, LOCAL) is None

    def test_digit_grouping_is_accepted(self, client: TestClient) -> None:
        assert self._set(client, "1,000,000").status_code == 303
        assert _call(client, usage.user_override, LOCAL) == usage.UserOverride(1_000_000)

    @pytest.mark.parametrize("value", ["-3", "0", "lots", "1.5"])
    def test_a_bad_budget_is_refused(self, client: TestClient, value: str) -> None:
        resp = self._set(client, value)
        assert resp.status_code == 400
        assert "positive whole number" in resp.text
        assert _call(client, usage.user_override, LOCAL) is None

    def test_another_guid_is_not_found(self, client: TestClient) -> None:
        assert self._set(client, "100", guid="someone-else").status_code == 404
        assert _call(client, usage.user_overrides_all_users) == {}

    def test_role_budgets_are_not_offered(self, client: TestClient) -> None:
        resp = client.post(
            "/admin/usage/budgets/roles",
            data={"role": "user", "monthly_tokens": "100"},
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _call(client, usage.role_budgets) == {}


class TestPages:
    def test_personal_mode_links_the_usage_view(self, client: TestClient) -> None:
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert 'href="/admin/usage"' in page.text

    @pytest.mark.parametrize("template", ["chat.html", "debate.html", "panel.html"])
    def test_the_budget_banner_is_on_every_turn_page(self, template: str) -> None:
        from pathlib import Path

        import spark.web

        text = (Path(spark.web.__file__).parent / "templates" / template).read_text("utf-8")
        assert '<script src="/static/js/budget.js"></script>' in text
        assert "data-budget-anchor" in text

    def test_the_banner_never_inserts_html(self) -> None:
        from pathlib import Path

        import spark.web

        script = (Path(spark.web.__file__).parent / "static" / "js" / "budget.js").read_text(
            "utf-8"
        )
        assert "innerHTML" not in script and "insertAdjacentHTML" not in script
        assert "textContent" in script

    def test_the_banner_script_is_served(self, client: TestClient) -> None:
        resp = client.get("/static/js/budget.js")
        assert resp.status_code == 200
        assert "/api/usage/me" in resp.text
