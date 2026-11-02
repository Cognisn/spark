"""The audit viewer and export in server mode (``spark.audit.read``, ``spark.audit.export``).

The real app on a PostgreSQL clone at head; the events are the ones warden
records for synthetic users' sign-ins and administration. Guards under test:

* reading the trail needs ``spark.audit.read`` and exporting it
  ``spark.audit.export`` (an API key scoped to reading alone cannot export);
* filters by actor, action, outcome and time range, and paging, return what
  they say;
* the export streams CSV or NDJSON through warden's export, which records the
  export itself;
* an Auditor sees the audit trail and no other administration.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.identity.permissions import ADMINISTRATOR, AUDIT_READ, AUDITOR, POWER_USER, USER
from spark.web.endpoints.audit import BAD_FILTER
from tests.identity_screen_helpers import NAVIGATE, plain_client, server_db  # noqa: F401
from tests.identity_web_helpers import PUBLIC, another_client, make_user, runtime, sign_in

pytestmark = pytest.mark.postgres

AUDITOR_EMAIL = "auditor@example.com"
ADA = "ada@example.com"


@pytest.fixture
def auditor(plain_client: TestClient) -> TestClient:
    make_user(plain_client, AUDITOR_EMAIL, AUDITOR)
    assert sign_in(plain_client, AUDITOR_EMAIL).status_code == 303
    return plain_client


def _rows(page: Any) -> list[str]:
    return re.findall(r'<tr class="audit-row"[^>]*data-action="([^"]+)"', page.text)


def _audit(client: TestClient, query: str = "") -> Any:
    page = client.get(f"/audit{query}", headers=NAVIGATE, follow_redirects=False)
    assert page.status_code == 200, (page.status_code, page.text[:300])
    return page


class TestAccess:
    @pytest.mark.parametrize("role", [USER, POWER_USER])
    def test_other_roles_are_refused(self, plain_client: TestClient, role: str) -> None:
        make_user(plain_client, ADA, role)
        sign_in(plain_client, ADA)
        assert plain_client.get("/audit", headers=NAVIGATE).status_code == 403
        assert plain_client.get("/audit/export?format=csv").status_code == 403

    def test_an_auditor_sees_no_other_administration(self, auditor: TestClient) -> None:
        assert _audit(auditor).status_code == 200
        for path in (
            "/admin/users",
            "/admin/invitations",
            "/admin/api-keys",
            "/settings/api/secrets",
        ):
            assert auditor.get(path, headers=NAVIGATE).status_code in (403, 404), path
        assert auditor.post("/admin/invitations", data={"email": ADA}).status_code == 403

    def test_an_administrator_reads_it_too(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, ADMINISTRATOR)
        sign_in(plain_client, ADA)
        assert _audit(plain_client).status_code == 200

    def test_reading_alone_does_not_export(self, auditor: TestClient) -> None:
        identity = runtime(auditor).identity

        async def key() -> str:
            user = await identity.users.get_by_email(AUDITOR_EMAIL)
            created = await identity.api_keys.create(user.id, "read only", {AUDIT_READ}, actor="t")
            result: str = created.key
            return result

        token = auditor.portal.call(key)  # type: ignore[attr-defined]
        bearer = TestClient(auditor.app, base_url=PUBLIC)
        bearer.portal = auditor.portal  # type: ignore[attr-defined]
        headers = {"Authorization": f"Bearer {token}"}
        assert bearer.get("/audit", headers=headers).status_code == 200
        assert bearer.get("/audit/export?format=csv", headers=headers).status_code == 403


class TestFilters:
    def test_by_action_and_outcome(self, auditor: TestClient) -> None:
        assert sign_in(another_client(auditor), "nobody@example.com").status_code == 401
        failed = _audit(auditor, "?action=warden.identity.sign_in&outcome=failure")
        assert _rows(failed) == ["warden.identity.sign_in"]
        succeeded = _audit(auditor, "?action=warden.identity.sign_in&outcome=success")
        assert _rows(succeeded) == ["warden.identity.sign_in"]  # the auditor's own

    def test_by_actor_email(self, auditor: TestClient) -> None:
        make_user(auditor, ADA)
        sign_in(another_client(auditor), ADA)
        page = _audit(auditor, f"?actor={ADA}")
        assert _rows(page) and set(_rows(page)) == {"warden.identity.sign_in"}
        assert _rows(_audit(auditor, "?actor=nobody%40example.com")) == []

    def test_by_time_range(self, auditor: TestClient) -> None:
        today = datetime.now(UTC).date()
        tomorrow, yesterday = today + timedelta(days=1), today - timedelta(days=1)
        assert _rows(_audit(auditor, f"?since={tomorrow}")) == []
        assert _rows(_audit(auditor, f"?until={yesterday}")) == []
        assert _rows(_audit(auditor, f"?since={today}&until={today}"))

    def test_a_bad_filter_is_explained(self, auditor: TestClient) -> None:
        page = auditor.get("/audit?since=not-a-date&outcome=maybe", headers=NAVIGATE)
        assert page.status_code == 400
        assert BAD_FILTER in page.text and _rows(page) == []

    def test_pages(self, auditor: TestClient) -> None:
        for n in range(30):  # each user: created, and a role assigned
            make_user(auditor, f"user{n:02d}@example.com")
        first = _audit(auditor)
        assert len(_rows(first)) == 50
        older = re.search(r'href="/audit\?([^"]*before_id=\d+[^"]*)"', first.text)
        assert older
        second = _audit(auditor, "?" + older.group(1).replace("&amp;", "&"))
        assert 0 < len(_rows(second)) <= 50
        ids = lambda page: re.findall(r'data-id="(\d+)"', page.text)  # noqa: E731
        assert min(map(int, ids(first))) > max(map(int, ids(second)))


class TestExport:
    def test_csv_is_an_attachment_and_is_audited(self, auditor: TestClient) -> None:
        resp = auditor.get("/audit/export?format=csv&action=warden.identity.sign_in")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        lines = resp.text.splitlines()
        assert lines[0].startswith("id,recorded_at,occurred_at,action")
        assert all(",warden.identity.sign_in," in line for line in lines[1:])
        recorded = _audit(auditor, "?action=warden.audit.export")
        assert _rows(recorded) == ["warden.audit.export"]

    def test_ndjson(self, auditor: TestClient) -> None:
        resp = auditor.get("/audit/export?format=ndjson&outcome=success")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in resp.text.splitlines()]
        assert events and all(e["outcome"] == "success" for e in events)

    def test_an_unknown_format_is_refused(self, auditor: TestClient) -> None:
        assert auditor.get("/audit/export?format=xlsx").status_code == 400
