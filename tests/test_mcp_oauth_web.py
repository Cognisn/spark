"""OAuth MCP servers through the web interface: the form, sign-in, the callback, sign-out.

Guards under test: the OAuth form fields are checked as conduit checks them; the
client secret goes to the secrets store and never back to the page; a sign-in
started on the settings page and completed at the public callback, in the browser
holding the binding cookie, stores tokens the MCP server then receives; a callback
without that cookie, or with a tampered state or address, stores nothing; sign-out
and the server's removal drop the tokens; no token, code or secret reaches a page,
a log or a user bundle. The callback is public in both modes. Personal mode runs
on SQLite against the local stub MCP server (tests.mcp_stub_server) behind a
synthetic authorisation server (tests.mcp_oauth_helpers); server mode on
PostgreSQL.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
import yaml
from conduit.integrations.konfig import KonfigSecretResolver
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings
from markupsafe import escape

from spark import mode
from spark.identity.permissions import ADMINISTRATOR
from spark.identity.web import WardenAuthenticator
from spark.instance_settings import access, accessor, audit
from spark.mcp_integration import oauth
from spark.mcp_integration.client import MCPClient
from spark.mcp_integration.manager import MCPManager
from spark.mcp_integration.runtime import MCPRuntime
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.web.security import CSRF_HEADER
from spark.web.server import _is_public, attach_database, create_app
from tests.identity_helpers import AsIdentity, Holder, identity_for
from tests.mcp_oauth_helpers import CLIENT_SECRET, ISSUER, MemorySecrets, SyntheticAuthServer
from tests.mcp_stub_server import StubServer, serving
from tests.web_helpers import login_token, sign_in

BASE = "http://127.0.0.1:22400"
PUBLIC_BASE = "https://spark.example.com"


class Ctx:
    def __init__(self, config: Path) -> None:
        self.settings = Settings(
            config_file=config, defaults={"interface": {"session_timeout_minutes": 60}}
        )
        self.secrets = MemorySecrets()


def _sign_in_at(client: TestClient, origin: str = BASE) -> None:
    """tests.web_helpers.sign_in, from ``origin`` (the loopback address OAuth needs)."""
    from spark.web.auth import SESSION_COOKIE
    from spark.web.security import CSRF_FORM_FIELD

    app: Any = client.app
    code = app.state.auth.generate_code()
    resp = client.post(
        "/api/auth",
        data={"code": code, CSRF_FORM_FIELD: login_token(client)},
        headers={"Origin": origin},
        follow_redirects=False,
    )
    session_id = resp.cookies.get(SESSION_COOKIE, "")
    assert session_id, "not signed in"
    client.cookies.set(SESSION_COOKIE, session_id)
    token = app.state.session.csrf_token(session_id) or ""
    client.headers.update({"Origin": origin, CSRF_HEADER: token})


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()
    accessor.reset()
    access.reset()
    audit.set_audit_hook(None)


@pytest.fixture(scope="module")
def stub() -> Iterator[StubServer]:
    with serving() as server_:
        yield server_


def _install(app: Any, auth: SyntheticAuthServer) -> MCPManager:
    """A manager whose HTTP goes through the synthetic authorisation server."""
    ctx = app.state.ctx
    resolver = KonfigSecretResolver(ctx.secrets)
    manager: MCPManager

    def make(config: Any) -> MCPClient:
        return MCPClient(
            config,
            secrets=resolver,
            tokens=manager.token_store(config.name),
            http_transport=auth.transport(httpx2.AsyncHTTPTransport()),
        )

    manager = MCPManager(make, secrets=resolver, tokens=ctx.secrets)
    runtime = MCPRuntime.start(manager)
    app.state.mcp_runtime = runtime
    app.state._mcp_loop = runtime.loop
    app.state.mcp_manager = manager
    return manager


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mcp": {"servers": []}}), encoding="utf-8", newline="\n")
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    return path


@pytest.fixture
def personal(
    config_file: Path, sqlite_settings: Any, stub: StubServer
) -> Iterator[tuple[TestClient, SyntheticAuthServer]]:
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    app = create_app(Ctx(config_file))  # type: ignore[arg-type]
    attach_database(app, sqlite_settings, True)
    with TestClient(app, base_url=BASE) as client:
        _sign_in_at(client)
        auth = SyntheticAuthServer(stub.http_url)
        _install(app, auth)
        yield client, auth


def _save(client: TestClient, stub: StubServer, **extra: Any) -> Any:
    return client.post(
        "/settings/mcp/api/save",
        json={
            "name": "docs",
            "transport": "http",
            "url": stub.http_url,
            "auth_type": "oauth",
            **extra,
        },
    )


def _sign_in(client: TestClient, auth: SyntheticAuthServer, **kw: Any) -> tuple[str, Any]:
    started = client.post("/settings/mcp/api/docs/oauth/sign-in")
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "redirect"
    assert oauth.BINDING_COOKIE in started.cookies
    return auth.authorise(body["authorization_url"], **kw), started


def _callback(client: TestClient, url: str, **kw: Any) -> Any:
    parts = urlsplit(url)
    return client.get(f"{parts.path}?{parts.query}", **kw)


def _secrets(client: TestClient) -> MemorySecrets:
    return client.app.state.ctx.secrets  # type: ignore[attr-defined,no-any-return]


def _status(client: TestClient) -> dict[str, Any]:
    servers = client.get("/settings/mcp/api/list").json()
    return next(s for s in servers if s["name"] == "docs")


def _wait_connected(client: TestClient, timeout: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _status(client)["connected"]:
            return True
        time.sleep(0.1)
    return False


class TestPublicCallback:
    def test_the_callback_is_public_in_both_modes(self) -> None:
        assert WardenAuthenticator.is_public(oauth.CALLBACK_PATH)
        assert _is_public(oauth.CALLBACK_PATH)

    def test_a_signed_out_browser_reaches_the_callback_and_is_refused(
        self, personal: tuple[TestClient, SyntheticAuthServer]
    ) -> None:
        client, _ = personal
        stranger = TestClient(client.app, base_url=BASE)
        resp = stranger.get(f"{oauth.CALLBACK_PATH}?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 400
        assert "did not succeed" in resp.text
        assert resp.headers["cache-control"] == "no-store"


class TestPersonalForm:
    def test_client_secret_is_stored_and_never_returned(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer, config_file: Path
    ) -> None:
        client, _ = personal
        resp = _save(
            client,
            stub,
            oauth_client_id="spark",
            oauth_client_secret=CLIENT_SECRET,
            oauth_issuer=ISSUER,
            oauth_scopes="tools:read",
        )
        assert resp.status_code == 200, resp.text
        assert CLIENT_SECRET in _secrets(client).data.values()
        assert CLIENT_SECRET not in config_file.read_text()
        for path in ("/settings/mcp/api/list", "/settings/mcp/api/docs/config", "/settings"):
            assert CLIENT_SECRET not in client.get(path).text
        shown = client.get("/settings/mcp/api/docs/config").json()
        assert shown["secrets_set"] == {"oauth_client_secret": True}
        assert "oauth_client_secret" not in shown
        assert shown["oauth_scopes"] == ["tools:read"]

    def test_secret_is_kept_replaced_and_removed(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, _ = personal
        fields = {"oauth_client_id": "spark", "oauth_issuer": ISSUER}
        _save(client, stub, oauth_client_secret=CLIENT_SECRET, **fields)
        assert _save(client, stub, **fields).status_code == 200  # kept
        assert CLIENT_SECRET in _secrets(client).data.values()
        _save(client, stub, oauth_client_secret="replacement-dev-only", **fields)
        values = _secrets(client).data.values()
        assert "replacement-dev-only" in values and CLIENT_SECRET not in values
        _save(client, stub, clear_secrets=["oauth_client_secret"], **fields)
        assert "replacement-dev-only" not in _secrets(client).data.values()
        shown = client.get("/settings/mcp/api/docs/config").json()
        assert shown["secrets_set"] == {"oauth_client_secret": False}

    def test_a_secret_without_its_issuer_is_refused(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, _ = personal
        resp = _save(client, stub, oauth_client_id="spark", oauth_client_secret=CLIENT_SECRET)
        assert resp.status_code == 400
        assert "issuer" in resp.json()["error"]
        assert CLIENT_SECRET not in _secrets(client).data.values()
        assert CLIENT_SECRET not in resp.text

    def test_oauth_on_stdio_is_refused(
        self, personal: tuple[TestClient, SyntheticAuthServer]
    ) -> None:
        client, _ = personal
        resp = client.post(
            "/settings/mcp/api/save",
            json={"name": "docs", "transport": "stdio", "command": "x", "auth_type": "oauth"},
        )
        assert resp.status_code == 400

    def test_page_offers_oauth(self, personal: tuple[TestClient, SyntheticAuthServer]) -> None:
        client, _ = personal
        page = client.get("/settings").text
        assert 'value="oauth"' in page and 'id="mcp-oauth-client-id"' in page


class TestPersonalSignIn:
    def test_round_trip_reaches_the_server_with_the_token(
        self,
        personal: tuple[TestClient, SyntheticAuthServer],
        stub: StubServer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client, auth = personal
        _save(client, stub)
        assert _status(client)["oauth"]["state"] == "needs_sign_in"
        callback, started = _sign_in(client, auth)
        page = _callback(client, callback)
        assert page.status_code == 200, page.text
        assert "Signed in to the MCP server" in page.text
        assert _status(client)["oauth"]["state"] == "signed_in"
        assert _wait_connected(client)
        access = [v for v in auth.issued if v.startswith("access-")][-1]
        assert {"authorization": f"Bearer {access}"} in stub.headers
        # Nothing secret reached a page or a log.
        state = parse_qs(urlsplit(callback).query)["state"][0]
        shown = page.text + started.text + client.get("/settings/mcp/api/list").text
        # SPARK's and conduit's logs (the test client's own request log, the browser's
        # side, is left out).
        logged = " | ".join(
            r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
        )
        for value in auth.issued:
            if not value.startswith("client-"):
                assert value not in shown + logged
        assert state not in page.text + logged
        assert started.cookies[oauth.BINDING_COOKIE] not in logged

    def test_without_the_binding_cookie_nothing_is_stored(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        _save(client, stub)
        callback, _ = _sign_in(client, auth)
        client.cookies.delete(oauth.BINDING_COOKIE, path=oauth.CALLBACK_PATH)
        page = _callback(client, callback)
        assert page.status_code == 400 and "No sign-in was waiting" in page.text
        assert _status(client)["oauth"]["state"] == "needs_sign_in"

    def test_a_tampered_state_is_refused(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        _save(client, stub)
        callback, _ = _sign_in(client, auth)
        parts = urlsplit(callback)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query["state"] = "tampered"
        page = client.get(f"{parts.path}?{urlencode(query)}")
        assert page.status_code == 400
        assert str(escape(oauth.reason_text("state_mismatch"))) in page.text
        status = _status(client)["oauth"]
        assert status["state"] == "failed" and status["reason"] == "state_mismatch"

    def test_a_callback_at_another_address_is_refused(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        _save(client, stub)
        callback, started = _sign_in(client, auth)
        parts = urlsplit(callback)
        nonce = started.cookies[oauth.BINDING_COOKIE]
        elsewhere = TestClient(client.app, base_url="http://localhost:22401")
        page = elsewhere.get(
            f"{parts.path}?{parts.query}", headers={"cookie": f"{oauth.BINDING_COOKIE}={nonce}"}
        )
        assert page.status_code == 400
        assert str(escape(oauth.reason_text("redirect_mismatch"))) in page.text
        assert _status(client)["oauth"]["state"] == "failed"

    def test_a_server_without_discovery_metadata_shows_the_reason(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        auth.publish_metadata = False
        _save(client, stub)
        resp = client.post("/settings/mcp/api/docs/oauth/sign-in")
        assert resp.status_code == 400
        assert resp.json()["reason"] == "discovery"
        assert _status(client)["oauth"]["reason"] == "discovery"

    def test_sign_out_removes_the_tokens(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        _save(client, stub)
        callback, _ = _sign_in(client, auth)
        assert _callback(client, callback).status_code == 200
        assert client.post("/settings/mcp/api/docs/oauth/sign-out").json() == {"status": "ok"}
        assert _status(client)["oauth"]["state"] == "needs_sign_in"
        assert not oauth.token_store(_secrets(client), "docs").signed_in()
        held = "".join(_secrets(client).data.values())
        assert not any(v in held for v in auth.issued if v.startswith(("access-", "refresh-")))

    def test_a_changed_url_drops_the_tokens(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        _save(client, stub)
        callback, _ = _sign_in(client, auth)
        assert _callback(client, callback).status_code == 200
        client.post(
            "/settings/mcp/api/save",
            json={
                "name": "docs",
                "transport": "http",
                "url": stub.sse_url,
                "auth_type": "oauth",
            },
        )
        assert _status(client)["oauth"]["state"] == "needs_sign_in"

    def test_removing_the_server_drops_tokens_and_secret(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = personal
        auth.pre_register("spark", CLIENT_SECRET)
        _save(
            client,
            stub,
            oauth_client_id="spark",
            oauth_client_secret=CLIENT_SECRET,
            oauth_issuer=ISSUER,
        )
        callback, _ = _sign_in(client, auth)
        assert _callback(client, callback).status_code == 200
        assert client.delete("/settings/mcp/api/docs").status_code == 200
        assert _secrets(client).data == {}

    def test_sign_in_needs_the_csrf_token(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, _ = personal
        _save(client, stub)
        token = client.headers.pop(CSRF_HEADER)
        try:
            assert client.post("/settings/mcp/api/docs/oauth/sign-in").status_code == 403
            assert client.post("/settings/mcp/api/docs/oauth/sign-out").status_code == 403
        finally:
            client.headers[CSRF_HEADER] = token

    def test_a_server_without_oauth_has_no_sign_in(
        self, personal: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, _ = personal
        client.post(
            "/settings/mcp/api/save",
            json={"name": "docs", "transport": "http", "url": stub.http_url, "auth_type": "none"},
        )
        assert client.post("/settings/mcp/api/docs/oauth/sign-in").status_code == 400
        assert client.post("/settings/mcp/api/nothing/oauth/sign-in").status_code == 404


# -- Bundles ---------------------------------------------------------------------------------


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


def test_tokens_are_not_in_a_user_bundle(
    tmp_path: Path, sqlite_settings: Any, config_file: Path
) -> None:
    from spark import files as files_pkg
    from spark.skills import manager as skills_manager

    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "files")}),
        data_dir=tmp_path,
        runtime_mode=Mode.PERSONAL,
    )
    skills_manager.set_skills_manager(skills_manager.SkillsManager(tmp_path / "skills"))
    secrets = MemorySecrets()
    held = oauth.token_store(secrets, "docs")
    from conduit.core.backends import TokenSet
    from pydantic import SecretStr

    tokens = TokenSet(
        access_token=SecretStr("access-bundle-dev-only"),
        refresh_token=SecretStr("refresh-bundle-dev-only"),
    )
    asyncio.run(held.put("mcp:docs", tokens))
    ctx = MagicMock()
    ctx.settings.get = lambda key, default=None, **_: {"interface.session_timeout_minutes": 60}.get(
        key, default
    )
    ctx.secrets = secrets
    app = create_app(ctx, first_run=False)
    attach_database(app, sqlite_settings, True)
    try:
        with TestClient(app) as client:
            sign_in(client)
            resp = client.get("/bundles/export")
            assert resp.status_code == 200, resp.text
            with zipfile.ZipFile(io.BytesIO(resp.content)) as bundle:
                for name in bundle.namelist():
                    data = bundle.read(name)
                    assert b"access-bundle-dev-only" not in data
                    assert b"refresh-bundle-dev-only" not in data
    finally:
        skills_manager.set_skills_manager(None)
        files_pkg.reset()


# -- Server mode -------------------------------------------------------------------------------


def _options() -> dict[str, Any]:
    return {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


@pytest.fixture
def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str, stub: StubServer
) -> Iterator[tuple[TestClient, SyntheticAuthServer]]:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"server": {"public_base_url": PUBLIC_BASE}}),
        encoding="utf-8",
        newline="\n",
    )
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    db = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    mode.configure(RuntimeSettings(Mode.SERVER, db))
    app = create_app(Ctx(path))  # type: ignore[arg-type]
    holder = Holder(identity_for(ADMINISTRATOR))
    app.add_middleware(AsIdentity, holder=holder)
    attach_database(app, db, True)
    with TestClient(app, backend_options=_options()) as client:
        sign_in(client)
        auth = SyntheticAuthServer(stub.http_url)
        _install(app, auth)
        yield client, auth


@pytest.mark.postgres
class TestServerMode:
    def _save(self, client: TestClient, stub: StubServer, **extra: Any) -> Any:
        return client.post(
            "/settings/mcp/api/save",
            json={
                "name": "docs",
                "transport": "http",
                "url": stub.http_url,
                "auth_type": "oauth",
                "roles": ["user"],
                **extra,
            },
        )

    def test_secret_goes_to_the_store_and_never_back(
        self, server: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, _ = server
        resp = self._save(
            client,
            stub,
            oauth_client_id="spark",
            oauth_client_secret=CLIENT_SECRET,
            oauth_issuer=ISSUER,
        )
        assert resp.status_code == 200, resp.text
        assert _secrets(client).data["mcp_server_docs_oauth_client_secret"] == CLIENT_SECRET
        stored = client.app.state.instance_settings.get("mcp.servers")  # type: ignore[attr-defined]
        assert CLIENT_SECRET not in str(stored)
        shown = client.get("/settings/mcp/api/docs/config")
        assert CLIENT_SECRET not in shown.text
        assert shown.json()["secrets_set"]["oauth_client_secret"] is True
        # A save without the secret keeps it, so the issuer is still required.
        refused = self._save(client, stub, oauth_client_id="spark")
        assert refused.status_code == 400 and "issuer" in refused.json()["error"]

    def test_a_secret_without_its_issuer_is_refused(
        self, server: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, _ = server
        resp = self._save(client, stub, oauth_client_id="spark", oauth_client_secret=CLIENT_SECRET)
        assert resp.status_code == 400
        assert "mcp_server_docs_oauth_client_secret" not in _secrets(client).data

    def test_round_trip_through_the_public_base_url(
        self, server: tuple[TestClient, SyntheticAuthServer], stub: StubServer
    ) -> None:
        client, auth = server
        assert self._save(client, stub).status_code == 200
        started = client.post("/settings/mcp/api/docs/oauth/sign-in")
        assert started.status_code == 200, started.text
        url = started.json()["authorization_url"]
        query = parse_qs(urlsplit(url).query)
        assert query["redirect_uri"] == [PUBLIC_BASE + oauth.CALLBACK_PATH]
        cookie = started.headers["set-cookie"]
        assert "secure" in cookie.lower() and "samesite=lax" in cookie.lower()
        assert "httponly" in cookie.lower() and f"path={oauth.CALLBACK_PATH}" in cookie.lower()
        nonce = started.cookies[oauth.BINDING_COOKIE]
        callback = urlsplit(auth.authorise(url))
        page = client.get(
            f"{callback.path}?{callback.query}",
            headers={"cookie": f"{oauth.BINDING_COOKIE}={nonce}"},
        )
        assert page.status_code == 200, page.text
        assert _status(client)["oauth"]["state"] == "signed_in"
        stored = client.app.state.instance_settings.get("mcp.servers")  # type: ignore[attr-defined]
        assert stored[0]["oauth_session"]
        assert "oauth_session" not in client.get("/settings/mcp/api/docs/config").json()
        assert client.post("/settings/mcp/api/docs/oauth/sign-out").status_code == 200
        assert _status(client)["oauth"]["state"] == "needs_sign_in"
