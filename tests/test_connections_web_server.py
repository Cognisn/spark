"""The connections screens in server mode: administrators only, metering, assignments, audit.

The real app on a PostgreSQL clone at head with warden's chain and synthetic
users, over a real connection runtime (conduit's adapters and its stub; no
network). Guards under test:

* every connections route needs ``spark.admin.connections``: 401 signed out, 403
  for every role but Administrator, and the Account menu lists the page only for it;
* a connection assigned to no one is offered to administrators of connections only;
* secrets go to the secrets store, never to the database or an answer;
* metering is saved and its worked example is credit-metering's estimate;
* assignments to roles and users, with a monthly allowance that a metered
  connection requires unless No limit is chosen explicitly (decision 88);
* every change is audited through warden by field name, never by value.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conduit.integrations.konfig import KonfigSecretResolver
from fastapi.testclient import TestClient
from warden.audit import AuditQuery, list_events
from warden.audit.store import AuditRow

from spark.connections import discovery
from spark.identity import audit as spark_audit
from spark.identity.db import warden_database
from spark.identity.permissions import ADMINISTRATOR, AUDITOR, POWER_USER, USER
from spark.usage import credits
from spark.web.security import CSRF_HEADER
from tests.identity_screen_helpers import NAVIGATE, plain_client, server_db  # noqa: F401
from tests.identity_web_helpers import another_client, make_user, sign_in
from tests.test_connections_web import (
    API,
    DEPLOYMENT,
    FOUNDRY_ENDPOINT,
    KEY,
    connection_runtime,
)

pytestmark = pytest.mark.postgres

ROOT = "root@example.com"
ADA = "ada@example.com"
FETCH = {"Accept": "application/json"}


def _guid(client: TestClient) -> str:
    return str(client.get("/auth/session", headers=FETCH).json()["user_guid"])


def _signed_in(client: TestClient, email: str, *roles: str) -> TestClient:
    browser = another_client(client)
    make_user(client, email, *roles)
    assert sign_in(browser, email).status_code == 303
    return browser


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


@pytest.fixture
def admin(plain_client: TestClient) -> Iterator[TestClient]:  # noqa: F811
    app: Any = plain_client.app
    runtime = connection_runtime(app.state.ctx.secrets)
    app.state.connections = runtime
    discovery.RESULTS.clear()
    make_user(plain_client, ROOT, ADMINISTRATOR)
    assert sign_in(plain_client, ROOT).status_code == 303
    try:
        yield plain_client
    finally:
        runtime.close()
        discovery.RESULTS.clear()


def _ollama(client: TestClient, name: str = "Lab box") -> str:
    resp = client.post(API, json={"service": "ollama", "name": name, "values": {}})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


ROUTES = (
    ("get", API),
    ("get", f"{API}/services"),
    ("post", API),
    ("get", f"{API}/box"),
    ("put", f"{API}/box"),
    ("delete", f"{API}/box"),
    ("post", f"{API}/box/enabled"),
    ("post", f"{API}/box/rename"),
    ("get", f"{API}/box/models"),
    ("put", f"{API}/box/models"),
    ("post", f"{API}/box/discover"),
    ("post", f"{API}/box/overlays"),
    ("get", f"{API}/box/metering"),
    ("put", f"{API}/box/metering"),
    ("get", f"{API}/box/assignments"),
    ("put", f"{API}/box/assignments"),
    ("delete", f"{API}/box/assignments?role=user"),
    ("get", "/admin/connections"),
)


def _request(client: TestClient, method: str, path: str) -> Any:
    kwargs: dict[str, Any] = {"json": {}} if method in ("post", "put") else {}
    return getattr(client, method)(path, **kwargs)


class TestAccess:
    @pytest.mark.parametrize("role", [USER, POWER_USER, AUDITOR])
    def test_every_other_role_is_refused_everywhere(self, admin: TestClient, role: str) -> None:
        _ollama(admin, "box")
        other = _signed_in(admin, ADA, role)
        for method, path in ROUTES:
            assert _request(other, method, path).status_code == 403, (method, path)
        # Nothing changed.
        assert admin.get(f"{API}/box").json()["name"] == "box"

    def test_signed_out_is_refused(self, admin: TestClient) -> None:
        anonymous = TestClient(admin.app, base_url=str(admin.base_url))
        anonymous.portal = admin.portal  # type: ignore[attr-defined]
        assert anonymous.get(API, headers=FETCH).status_code == 401

    def test_the_administrators_page_and_menu(self, admin: TestClient) -> None:
        page = admin.get("/admin/connections", headers=NAVIGATE)
        assert page.status_code == 200
        assert 'id="connections-app" data-server="true"' in page.text
        assert 'href="/admin/connections"' in page.text
        other = _signed_in(admin, ADA, POWER_USER)
        assert 'href="/admin/connections"' not in other.get("/", headers=NAVIGATE).text


def _stub(client: TestClient) -> None:
    """A stub connection in the database (the modal offers only M4's services)."""
    from spark.connections.startup import loader
    from spark.connections.store import DatabaseConnectionStore
    from tests.model_helpers import stub_connection

    app: Any = client.app

    async def save(db: Any) -> None:
        await DatabaseConnectionStore(db).save_connection(stub_connection("lab"))

    app.state.sync_db.call(save)
    app.state.connections.refresh(loader(app.state.sync_db, server=True, config_path=None))


def _offered(client: TestClient) -> set[str]:
    resp = client.get("/conversations/api/models", headers=FETCH)
    assert resp.status_code == 200, resp.text
    return {str(m["id"]) for m in resp.json()["models"]}


class TestUnassignedConnections:
    def test_only_administrators_of_connections_are_offered_its_models(
        self, admin: TestClient
    ) -> None:
        _stub(admin)
        other = _signed_in(admin, ADA, POWER_USER)
        assert "lab/stub-echo" in _offered(admin)
        assert "lab/stub-echo" not in _offered(other)
        admin.put(f"{API}/lab/assignments", json={"role": POWER_USER})
        assert "lab/stub-echo" in _offered(other)
        assert "lab/stub-echo" not in _offered(admin)  # assigned now, and not to them


class TestSecrets:
    def test_secrets_reach_the_store_and_never_the_database(self, admin: TestClient) -> None:
        resp = admin.post(
            API,
            json={"service": "anthropic", "name": "Claude", "secrets": {"api_key": KEY}},
        )
        assert resp.status_code == 201, resp.text
        app: Any = admin.app
        assert app.state.ctx.secrets.get("connections.claude.api_key") == KEY
        stored = app.state.sync_db.fetch_value(
            "SELECT settings FROM connections WHERE id = ?", ("claude",)
        )
        assert KEY not in stored and "secret://connections.claude.api_key" in stored
        assert admin.get(f"{API}/claude").json()["secrets"] == {"api_key": True}
        assert KEY not in admin.get(API).text

    def test_changes_are_audited_by_field_name_only(self, admin: TestClient) -> None:
        admin.post(
            API, json={"service": "anthropic", "name": "Claude", "secrets": {"api_key": KEY}}
        )
        admin.put(f"{API}/claude", json={"values": {"base_url": "https://gw.example.com"}})
        admin.delete(f"{API}/claude")
        changes = [
            (e.resource_type, e.resource_id, e.details.get("change"), e.details.get("fields"))
            for e in _events(admin, spark_audit.SETTING_CHANGED)
            if e.resource_type == "connection"
        ]
        assert ("connection", "claude", "deleted", None) in changes
        created = [c for c in changes if c[2] == "created"][0]
        assert created[3] == ["api_key", "endpoint", "kind", "name", "residency"]
        changed = [c for c in changes if c[2] == "changed"][0]
        assert changed[3] == ["base_url"]
        secret = _events(admin, spark_audit.SECRET_CHANGED)
        assert [(e.resource_type, e.resource_id) for e in secret] == [
            ("connection_secret", "connections.claude.api_key")
        ]
        for event in _events(admin, spark_audit.SETTING_CHANGED) + secret:
            assert KEY not in str(event.details) and "gw.example.com" not in str(event.details)


class TestMetering:
    def test_saved_with_credit_meterings_worked_example(self, admin: TestClient) -> None:
        cid = _ollama(admin)
        shown = admin.get(f"{API}/{cid}/metering").json()
        assert shown["metered"] is False and shown["base_rate_tokens"] == 1000
        assert shown["defaults"]["output_weight"] == 5.0
        resp = admin.put(
            f"{API}/{cid}/metering",
            json={"metered": True, "base_rate_tokens": 500, "higher_multiplier": 3},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["metered"] is True and data["base_rate_tokens"] == 500
        from spark.database.connections import Metering

        expected = credits.estimate(Metering(cid, True, 500, 3.0), data["sample"]).as_dict()
        assert data["example"] == expected
        assert isinstance(data["example"]["credits"]["standard"], str)
        (item,) = admin.get(API).json()["connections"]
        assert item["metered"] is True
        audited = [
            e.details.get("fields")
            for e in _events(admin, spark_audit.SETTING_CHANGED)
            if e.details.get("change") == "metering"
        ]
        assert audited == [["metered", "base_rate_tokens", "higher_multiplier"]]

    def test_bad_rates_are_refused(self, admin: TestClient) -> None:
        cid = _ollama(admin)
        assert admin.put(f"{API}/{cid}/metering", json={"base_rate_tokens": 0}).status_code == 400
        assert admin.put(f"{API}/{cid}/metering", json={"output_weight": "x"}).status_code == 400


class TestAssignments:
    def _metered(self, admin: TestClient) -> str:
        cid = _ollama(admin)
        admin.put(f"{API}/{cid}/metering", json={"metered": True})
        return cid

    def test_a_metered_connection_needs_an_allowance_or_an_explicit_no_limit(
        self, admin: TestClient
    ) -> None:
        cid = self._metered(admin)
        blank = admin.put(f"{API}/{cid}/assignments", json={"role": USER})
        assert blank.status_code == 400 and "No limit" in blank.json()["error"]
        both = admin.put(
            f"{API}/{cid}/assignments",
            json={"role": USER, "monthly_credits": 5, "no_limit": True},
        )
        assert both.status_code == 400
        unlimited = admin.put(f"{API}/{cid}/assignments", json={"role": USER, "no_limit": True})
        assert unlimited.status_code == 200
        (row,) = unlimited.json()["assignments"]
        assert row["role"] == USER and row["monthly_credits"] is None and row["no_limit"] is True
        limited = admin.put(
            f"{API}/{cid}/assignments", json={"role": USER, "monthly_credits": "250"}
        )
        assert limited.json()["assignments"][0]["monthly_credits"] == 250

    def test_an_unmetered_connection_takes_no_allowance(self, admin: TestClient) -> None:
        cid = _ollama(admin)
        resp = admin.put(f"{API}/{cid}/assignments", json={"role": POWER_USER})
        assert resp.status_code == 200
        assert resp.json()["assignments"][0]["no_limit"] is True

    def test_users_and_roles_must_be_known(self, admin: TestClient) -> None:
        cid = _ollama(admin)
        assert admin.put(f"{API}/{cid}/assignments", json={"role": "chef"}).status_code == 400
        assert (
            admin.put(f"{API}/{cid}/assignments", json={"user_guid": "nobody"}).status_code == 400
        )
        both = {"role": USER, "user_guid": _guid(admin)}
        assert admin.put(f"{API}/{cid}/assignments", json=both).status_code == 400

    def test_a_user_assignment_by_email_then_removed(self, admin: TestClient) -> None:
        cid = _ollama(admin)
        other = _signed_in(admin, ADA, USER)
        guid = _guid(other)
        data = admin.get(f"{API}/{cid}/assignments").json()
        assert {"user_guid": guid, "label": ADA} in data["users"]
        assert set(data["roles"]) == {ADMINISTRATOR, AUDITOR, POWER_USER, USER}
        resp = admin.put(f"{API}/{cid}/assignments", json={"user_guid": guid})
        assert resp.json()["assignments"][0]["user"] == ADA
        (item,) = admin.get(API).json()["connections"]
        assert item["assignments"] == 1
        gone = admin.delete(f"{API}/{cid}/assignments?user_guid={guid}")
        assert gone.status_code == 200 and gone.json()["assignments"] == []
        assert admin.delete(f"{API}/{cid}/assignments?user_guid={guid}").status_code == 404
        audited = [
            e.details.get("change")
            for e in _events(admin, spark_audit.SETTING_CHANGED)
            if e.details.get("connection_id") == cid
        ]
        assert "assignment" in audited and "unassigned" in audited


def _foundry(client: TestClient, name: str = "Foundry AU") -> str:
    resp = client.post(
        API,
        json={
            "service": "anthropic_foundry",
            "name": name,
            "values": {"base_url": FOUNDRY_ENDPOINT, "region": "australiaeast"},
            "secrets": {"api_key": KEY},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _overlays(client: TestClient) -> list[Any]:
    from spark.database.connections import list_overlays

    app: Any = client.app
    rows: list[Any] = app.state.sync_db.call(list_overlays)
    return rows


def _declared(client: TestClient) -> list[Any]:
    return [
        e.details.get("model_id")
        for e in _events(client, spark_audit.SETTING_CHANGED)
        if e.details.get("change") == "overlay_declared"
    ]


class TestDeclaringByID:
    """A Foundry deployment typed in by name, on PostgreSQL with warden's chain."""

    def test_an_administrator_declares_a_deployment_and_it_is_offered(
        self, admin: TestClient
    ) -> None:
        cid = _foundry(admin)
        assert f"{cid}/sonnet-au-prod" not in _offered(admin)
        resp = admin.post(f"{API}/{cid}/overlays", json=DEPLOYMENT)
        assert resp.status_code == 200, resp.text
        rows = {m["model_id"]: m for m in resp.json()["models"]}
        assert rows["sonnet-au-prod"]["overlay"] and rows["sonnet-au-prod"]["refused"] is None
        assert [(r.provider, r.model_id) for r in _overlays(admin)] == [
            ("anthropic", "sonnet-au-prod")
        ]
        assert f"{cid}/sonnet-au-prod" in _offered(admin)
        assert _declared(admin) == ["sonnet-au-prod"]
        tier = admin.put(
            f"{API}/{cid}/models", json={"model_id": "sonnet-au-prod", "tier": "lower"}
        )
        assert {m["model_id"]: m for m in tier.json()["models"]}["sonnet-au-prod"][
            "tier"
        ] == "lower"

    def test_an_id_the_catalogue_ships_is_refused_and_not_audited(self, admin: TestClient) -> None:
        cid = _foundry(admin)
        resp = admin.post(f"{API}/{cid}/overlays", json={**DEPLOYMENT, "model_id": "claude-opus-5"})
        assert resp.status_code == 409
        assert "already lists claude-opus-5" in resp.json()["error"]
        assert _overlays(admin) == [] and _declared(admin) == []

    @pytest.mark.parametrize("role", [USER, POWER_USER, AUDITOR])
    def test_any_other_role_is_refused_and_nothing_is_stored(
        self, admin: TestClient, role: str
    ) -> None:
        cid = _foundry(admin)
        other = _signed_in(admin, ADA, role)
        assert other.post(f"{API}/{cid}/overlays", json=DEPLOYMENT).status_code == 403
        assert _overlays(admin) == [] and _declared(admin) == []

    def test_declaring_needs_the_csrf_token(self, admin: TestClient) -> None:
        cid = _foundry(admin)
        token = admin.headers.pop(CSRF_HEADER)
        try:
            assert admin.post(f"{API}/{cid}/overlays", json=DEPLOYMENT).status_code == 403
        finally:
            admin.headers[CSRF_HEADER] = token
        assert _overlays(admin) == []


class TestDeclarationsStayOnTheirConnection:
    """Wave 10 Q53, on PostgreSQL: a declaration is stored for, and offered on, the
    declaring connection only."""

    def test_a_foundry_deployment_is_offered_on_its_own_connection_only(
        self, admin: TestClient
    ) -> None:
        au, us = _foundry(admin, "Foundry AU"), _foundry(admin, "Foundry US")
        direct = admin.post(
            API,
            json={"service": "anthropic", "name": "Claude direct", "secrets": {"api_key": KEY}},
        ).json()["id"]
        assert admin.post(f"{API}/{au}/overlays", json=DEPLOYMENT).status_code == 200
        assert [(r.model_id, r.connection_id) for r in _overlays(admin)] == [("sonnet-au-prod", au)]
        offered = _offered(admin)
        assert f"{au}/sonnet-au-prod" in offered
        assert f"{us}/sonnet-au-prod" not in offered
        assert f"{direct}/sonnet-au-prod" not in offered
        us_view = admin.get(f"{API}/{us}/models").json()["models"]
        assert "sonnet-au-prod" not in {m["model_id"] for m in us_view}

    def test_a_declared_ollama_model_is_offered_on_its_own_connection_only(
        self, admin: TestClient
    ) -> None:
        home, lab = _ollama(admin, "Home box"), _ollama(admin, "Lab box")
        body = {
            **DEPLOYMENT,
            "model_id": "qwen3:32b",
            "residency_scope": "local",
            "regions": "",
        }
        assert admin.post(f"{API}/{home}/overlays", json=body).status_code == 200
        offered = _offered(admin)
        assert f"{home}/qwen3:32b" in offered and f"{lab}/qwen3:32b" not in offered

    def test_each_connection_keeps_its_own_entry_and_the_409_holds(self, admin: TestClient) -> None:
        au, us = _foundry(admin, "Foundry AU"), _foundry(admin, "Foundry US")
        assert admin.post(f"{API}/{au}/overlays", json=DEPLOYMENT).status_code == 200
        us_body = {**DEPLOYMENT, "regions": "eastus", "context_window": 100000}
        assert admin.post(f"{API}/{us}/overlays", json=us_body).status_code == 200
        windows = {r.connection_id: r.context_window for r in _overlays(admin)}
        assert windows == {au: 200000, us: 100000}
        for cid in (au, us):
            shipped = admin.post(
                f"{API}/{cid}/overlays", json={**DEPLOYMENT, "model_id": "claude-opus-5"}
            )
            assert shipped.status_code == 409, cid
        assert admin.delete(f"{API}/{au}").status_code == 200
        assert [r.connection_id for r in _overlays(admin)] == [us]
