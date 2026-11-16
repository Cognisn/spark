"""The usage view, its export and a user's own standing on the web: personal mode.

Personal mode's single local user holds every permission, so the view is theirs.
It meters no credits (M3): calls are still charged at their connection's rates for
the record, but nothing is limited. Server mode's guards are in
``test_usage_web_server.py``.
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
from tests.credit_helpers import allow, metered
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


def _rows(page: Any) -> list[tuple[str, str, str, str, int, str]]:
    return [
        (m[0], m[1], m[2], m[3], int(m[4]), m[5])
        for m in re.findall(
            r'<tr class="usage-row" data-period="([^"]*)" data-user="([^"]*)"'
            r' data-connection="([^"]*)" data-model="([^"]*)" data-total="(\d+)"'
            r' data-credits="([^"]*)"',
            page.text,
        )
    ]


def _work(client: TestClient) -> None:
    """A connection at the default rates: 1 credit per 1,000 input tokens."""
    _call(client, metered, "work", name="Work Bedrock", rates={})


def _record(client: TestClient, model: str, tokens_in: int, tokens_out: int, **kw: Any) -> None:
    _call(client, usage.record_usage, 1, model, tokens_in, tokens_out, LOCAL, **kw)


class TestMyUsage:
    def test_personal_mode_meters_no_credits(self, client: TestClient) -> None:
        _work(client)
        _call(client, allow, "work", role="administrator", credits=1)
        _record(client, "m1", 10**6, 0, connection_id="work")
        data = client.get("/api/usage/me?connection=work").json()
        assert (data["state"], data["limit"], data["connection_id"]) == ("unlimited", None, "work")
        assert data["connections"] == {}

    def test_signed_in_without_a_connection(self, client: TestClient) -> None:
        data = client.get("/api/usage/me").json()
        assert (data["state"], data["period"], data["unit"]) == ("unlimited", PERIOD, "credits")


class TestView:
    def test_usage_by_month_user_connection_and_model(self, client: TestClient) -> None:
        _work(client)
        _record(client, "m1", 1000, 100, connection_id="work")
        _record(client, "m1", 2000, 100, connection_id="work")
        _record(client, "m2", 7, 0)
        _record(client, "m1", 99, 0, connection_id="work", at=OCTOBER)
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert page.status_code == 200
        assert _rows(page) == [
            (PERIOD, "Local user", "", "m2", 7, "0.000000"),
            (PERIOD, "Local user", "work", "m1", 3200, "4.000000"),
        ]
        assert "Work Bedrock" in page.text
        october = client.get("/admin/usage?month=2026-10", headers=NAVIGATE)
        assert _rows(october) == [("2026-10", "Local user", "work", "m1", 99, "0.099000")]
        every = client.get("/admin/usage?month=all", headers=NAVIGATE)
        assert len(_rows(every)) == 3

    def test_totals_per_connection(self, client: TestClient) -> None:
        _work(client)
        _record(client, "m1", 1000, 100, connection_id="work", cache_read_tokens=10_000)
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert re.search(
            r'<tr class="total-row" data-connection="work" data-credits="2.500000">', page.text
        )

    def test_a_bad_month_is_explained(self, client: TestClient) -> None:
        page = client.get("/admin/usage?month=2026-13", headers=NAVIGATE)
        assert page.status_code == 400
        assert "month" in page.text.lower()

    def test_no_standings_and_no_budget_forms(self, client: TestClient) -> None:
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert "standing-row" not in page.text
        assert "/admin/usage/budgets" not in page.text

    @pytest.mark.parametrize("path", ["/admin/usage/budgets/users", "/admin/usage/budgets/roles"])
    def test_the_token_budget_forms_are_gone(self, client: TestClient, path: str) -> None:
        resp = client.post(path, data={"monthly_tokens": "5"}, follow_redirects=False)
        assert resp.status_code in (404, 405)


class TestExport:
    def test_csv_of_one_month(self, client: TestClient) -> None:
        _work(client)
        _record(
            client,
            "m1",
            500,
            100,
            connection_id="work",
            cache_read_tokens=10_000,
            cache_write_tokens=2_000,
        )
        _record(client, "m1", 99, 0, connection_id="work", at=OCTOBER)
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
            "connection",
            "connection_id",
            "model",
            "requests",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "credits",
        ]
        assert rows[1:] == [
            [
                PERIOD,
                "Local user",
                LOCAL,
                "Work Bedrock",
                "work",
                "m1",
                "1",
                "500",
                "100",
                "10000",
                "2000",
                "12600",
                "4.500000",
            ]
        ]

    def test_every_month(self, client: TestClient) -> None:
        _record(client, "m1", 10, 5)
        _record(client, "m1", 99, 0, at=OCTOBER)
        rows = list(csv.reader(io.StringIO(client.get("/admin/usage/export.csv").text)))
        assert [r[0] for r in rows[1:]] == [PERIOD, "2026-10"]

    def test_cells_that_look_like_formulas_are_neutralised(self, client: TestClient) -> None:
        _record(client, '=HYPERLINK("http://example.com")', 1, 1)
        rows = list(csv.reader(io.StringIO(client.get("/admin/usage/export.csv").text)))
        assert rows[1][5].startswith("'=")


class TestPages:
    def test_personal_mode_links_the_usage_view(self, client: TestClient) -> None:
        page = client.get("/admin/usage", headers=NAVIGATE)
        assert 'href="/admin/usage"' in page.text

    @pytest.mark.parametrize("template", ["chat.html", "debate.html", "panel.html"])
    def test_the_credit_banner_is_on_every_turn_page(self, template: str) -> None:
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

    @pytest.mark.parametrize("template", ["chat.html", "conversations.html"])
    def test_the_model_picker_is_on_every_page_that_chooses_a_model(self, template: str) -> None:
        from pathlib import Path

        import spark.web

        text = (Path(spark.web.__file__).parent / "templates" / template).read_text("utf-8")
        assert '<script src="/static/js/model_picker.js"></script>' in text
        assert "SparkModelPicker." in text

    def test_the_picker_escapes_what_it_puts_in_html(self) -> None:
        from pathlib import Path

        import spark.web

        folder = Path(spark.web.__file__).parent / "static" / "js"
        script = (folder / "model_picker.js").read_text("utf-8")
        assert "innerHTML" not in script and "insertAdjacentHTML" not in script
        assert "escape(m.id)" in script and "escape(text)" in script
        assert "escape(label)" in script
