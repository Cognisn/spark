"""The MCP server settings API in server mode (A12, A9) and personal mode.

Guards under test: a stdio server is refused by the API with a clear message;
the servers are the ``mcp.servers`` instance setting, never ``config.yaml``;
credentials go to the secrets store and never back to the browser; only a
holder of ``spark.settings.instance`` may use the API; a change reconnects the
servers through the settings follower (this replica's, and the listener's on
others). Personal mode keeps ``config.yaml`` and stdio.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

from spark import mode
from spark.identity.permissions import ADMINISTRATOR, POWER_USER, USER
from spark.instance_settings import access, accessor, audit
from spark.mcp_integration.servers import STDIO_REFUSED
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.web.server import attach_database, create_app
from tests.db_helpers import app_client
from tests.identity_helpers import AsIdentity, Holder, identity_for
from tests.web_helpers import sign_in

TOKEN = "mcp-token-web-dev-only"


class MemorySecrets:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self.data

    def resolve_uri(self, value: str) -> str | None:
        return self.data.get(value[len("secret://") :]) if value.startswith("secret://") else value


class Ctx:
    def __init__(self, config: Path) -> None:
        self.settings = Settings(
            config_file=config, defaults={"interface": {"session_timeout_minutes": 60}}
        )
        self.secrets = MemorySecrets()


class Applied:
    """Stands in for the MCP runtime: records what the follower applies."""

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def apply(self, configs: list[Any]) -> set[str]:
        self.calls.append(list(configs))
        return {c.name for c in configs}


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()
    accessor.reset()
    access.reset()
    audit.set_audit_hook(None)


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    stdio = {"name": "local", "transport": "stdio", "command": "npx"}
    path.write_text(yaml.safe_dump({"mcp": {"servers": [stdio]}}), encoding="utf-8", newline="\n")
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    return path


def _http(name: str = "search", **extra: Any) -> dict[str, Any]:
    return {"name": name, "transport": "http", "url": "https://mcp.example.com/mcp", **extra}


def _options() -> dict[str, Any]:
    return {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


@pytest.fixture
def server(config_file: Path, pg_url: str) -> Iterator[tuple[TestClient, Holder, Applied]]:
    db = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    mode.configure(RuntimeSettings(Mode.SERVER, db))
    app = create_app(Ctx(config_file))  # type: ignore[arg-type]
    holder = Holder(identity_for(ADMINISTRATOR))
    app.add_middleware(AsIdentity, holder=holder)
    attach_database(app, db, True)
    applied = Applied()
    with TestClient(app, backend_options=_options()) as client:
        sign_in(client)
        import spark.web.server as server_mod

        instance = server_mod._server_consumers(app, app.state.ctx)
        assert instance is not None
        follower = instance.follow(_Llm(), None, applied)  # type: ignore[arg-type]
        follower.wait_idle()
        applied.calls.clear()
        app.state.settings_follower = follower
        yield client, holder, applied
        follower.wait_idle()


class _Llm:
    def replace_providers(self, providers: Any) -> None: ...

    def set_offered(self, offered: Any) -> None: ...


def _stored(client: TestClient) -> list[dict[str, Any]]:
    value = client.app.state.instance_settings.get("mcp.servers")  # type: ignore[attr-defined]
    return list(value)


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


@pytest.mark.postgres
class TestServerTransports:
    def test_save_refuses_stdio_with_a_clear_message(self, server: Any) -> None:
        client, _, _ = server
        resp = client.post(
            "/settings/mcp/api/save", json={"name": "local", "transport": "stdio", "command": "x"}
        )
        assert resp.status_code == 400
        assert STDIO_REFUSED in resp.json()["error"]
        assert _stored(client) == []

    def test_test_refuses_stdio(self, server: Any) -> None:
        client, _, _ = server
        resp = client.post(
            "/settings/mcp/api/test", json={"name": "local", "transport": "stdio", "command": "x"}
        )
        assert resp.status_code == 400
        assert STDIO_REFUSED in resp.json()["message"]

    def test_the_generic_settings_api_refuses_stdio_too(self, server: Any) -> None:
        client, _, _ = server
        resp = client.post(
            "/settings/api/instance",
            json={"mcp.servers": [{"name": "local", "transport": "stdio", "command": "x"}]},
        )
        assert resp.status_code == 400
        assert "stdio" in resp.text

    def test_config_yaml_servers_are_never_listed(self, server: Any) -> None:
        client, _, _ = server
        assert client.get("/settings/mcp/api/list").json() == []


@pytest.mark.postgres
class TestServerSave:
    def test_saves_into_the_instance_settings_with_roles(self, server: Any) -> None:
        client, _, applied = server
        resp = client.post("/settings/mcp/api/save", json=_http(roles=["user"]))
        assert resp.status_code == 200, resp.text
        stored = _stored(client)
        assert [(s["name"], s["roles"]) for s in stored] == [("search", ["user"])]
        assert _wait(lambda: any(c and c[0].name == "search" for c in applied.calls))

    def test_credentials_go_to_the_secrets_store_only(self, server: Any) -> None:
        client, _, applied = server
        resp = client.post(
            "/settings/mcp/api/save",
            json=_http(auth_type="bearer", auth_token=TOKEN, custom_headers={"X-Key": TOKEN}),
        )
        assert resp.status_code == 200, resp.text
        assert TOKEN not in str(_stored(client))
        secrets = client.app.state.ctx.secrets.data  # type: ignore[attr-defined]
        assert secrets["mcp_server_search_auth_token"] == TOKEN
        assert _wait(lambda: any(c and c[0].auth_token == TOKEN for c in applied.calls))
        for path in ("/settings/mcp/api/list", "/settings/mcp/api/search/config"):
            page = client.get(path)
            assert page.status_code == 200
            assert TOKEN not in page.text
        config = client.get("/settings/mcp/api/search/config").json()
        assert config["secrets_set"]["auth_token"] is True

    def test_an_empty_credential_keeps_the_stored_one(self, server: Any) -> None:
        client, _, _ = server
        client.post("/settings/mcp/api/save", json=_http(auth_type="bearer", auth_token=TOKEN))
        client.post("/settings/mcp/api/save", json=_http(auth_type="bearer", auth_token=""))
        secrets = client.app.state.ctx.secrets.data  # type: ignore[attr-defined]
        assert secrets["mcp_server_search_auth_token"] == TOKEN

    def test_cleared_credentials_are_removed(self, server: Any) -> None:
        client, _, _ = server
        client.post("/settings/mcp/api/save", json=_http(auth_type="bearer", auth_token=TOKEN))
        client.post("/settings/mcp/api/save", json=_http(clear_secrets=["auth_token"]))
        secrets = client.app.state.ctx.secrets.data  # type: ignore[attr-defined]
        assert "mcp_server_search_auth_token" not in secrets

    def test_unknown_role_is_refused(self, server: Any) -> None:
        client, _, _ = server
        resp = client.post("/settings/mcp/api/save", json=_http(roles=["superuser"]))
        assert resp.status_code == 400
        assert "role" in resp.json()["error"]

    def test_bad_name_is_refused(self, server: Any) -> None:
        client, _, _ = server
        assert client.post("/settings/mcp/api/save", json=_http("My Server")).status_code == 400

    def test_toggle_and_delete_change_the_setting(self, server: Any) -> None:
        client, _, _ = server
        client.post("/settings/mcp/api/save", json=_http(auth_type="bearer", auth_token=TOKEN))
        resp = client.post("/settings/mcp/api/toggle", json={"name": "search", "enabled": False})
        assert resp.status_code == 200
        assert _stored(client)[0]["enabled"] is False
        assert client.delete("/settings/mcp/api/search").status_code == 200
        assert _stored(client) == []
        secrets = client.app.state.ctx.secrets.data  # type: ignore[attr-defined]
        assert not any(k.startswith("mcp_server_search_") for k in secrets)

    def test_toggle_and_delete_of_an_unknown_server_answer_404(self, server: Any) -> None:
        client, _, _ = server
        assert (
            client.post("/settings/mcp/api/toggle", json={"name": "x", "enabled": True}).status_code
            == 404
        )
        assert client.delete("/settings/mcp/api/x").status_code == 404

    def test_roles_offered_to_the_editor(self, server: Any) -> None:
        client, _, _ = server
        roles = client.get("/settings/mcp/api/roles").json()
        assert {"administrator", "user", "power_user", "auditor"} <= set(roles)

    def test_the_change_is_audited_as_a_setting_and_a_secret(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _, _ = server
        changes: list[Any] = []
        audit.set_audit_hook(changes.append)
        client.post("/settings/mcp/api/save", json=_http(auth_type="bearer", auth_token=TOKEN))
        keys = {(c.key, c.secret) for c in changes}
        assert ("mcp.servers", False) in keys
        assert ("mcp.servers.search.auth_token", True) in keys
        assert TOKEN not in repr(changes)


@pytest.mark.postgres
class TestServerAccess:
    @pytest.mark.parametrize("role", [USER, POWER_USER])
    def test_only_instance_administrators_may_use_the_api(self, server: Any, role: str) -> None:
        client, holder, _ = server
        holder.identity = identity_for(role)
        for method, path, body in (
            ("get", "/settings/mcp/api/list", None),
            ("post", "/settings/mcp/api/save", _http()),
            ("post", "/settings/mcp/api/test", _http()),
            ("post", "/settings/mcp/api/toggle", {"name": "search", "enabled": True}),
            ("get", "/settings/mcp/api/search/config", None),
            ("get", "/settings/mcp/api/roles", None),
            ("delete", "/settings/mcp/api/search", None),
        ):
            call = getattr(client, method)
            resp = call(path, json=body) if body is not None else call(path)
            assert resp.status_code == 403, (method, path)

    def test_instance_page_has_the_editor_without_stdio(self, server: Any) -> None:
        client, _, _ = server
        page = client.get("/settings")
        assert page.status_code == 200
        assert 'id="section-mcp-servers"' in page.text
        assert 'value="stdio"' not in page.text
        assert 'id="mcp-roles"' in page.text


# -- Personal mode ----------------------------------------------------------------------


@pytest.fixture
def personal(config_file: Path, sqlite_settings: Any) -> Iterator[TestClient]:
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    app = create_app(Ctx(config_file))  # type: ignore[arg-type]
    with app_client(app, sqlite_settings) as client:
        sign_in(client)
        yield client


class TestPersonal:
    def test_lists_config_yaml_servers_including_stdio(self, personal: TestClient) -> None:
        servers = personal.get("/settings/mcp/api/list").json()
        assert [(s["name"], s["transport"]) for s in servers] == [("local", "stdio")]

    def test_saves_a_stdio_server_into_config_yaml(
        self, personal: TestClient, config_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.web.endpoints.mcp_servers as endpoint

        async def no_connect(request: Any, srv_config: dict[str, Any]) -> None:
            return None

        monkeypatch.setattr(endpoint, "_connect_server", no_connect)
        resp = personal.post(
            "/settings/mcp/api/save",
            json={"name": "Other Tools", "transport": "stdio", "command": "uvx", "args": ["x"]},
        )
        assert resp.status_code == 200
        names = [s["name"] for s in yaml.safe_load(config_file.read_text())["mcp"]["servers"]]
        assert names == ["local", "Other Tools"]

    def test_page_offers_stdio(self, personal: TestClient) -> None:
        page = personal.get("/settings")
        assert 'value="stdio"' in page.text
        assert 'id="mcp-roles"' not in page.text
