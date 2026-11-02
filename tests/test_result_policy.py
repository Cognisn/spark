"""Result actions for flagged tool results as an administrator's instance policy (D30, D36).

Guards under test: in server mode ``embedded_tools.tool_results.<source>`` are
administrator instance settings, saved only through ``/settings/api/instance``,
refused for a non-administrator and as a preference, and every change calls
``spark.safety.untrusted.audit("tool_result.policy_changed", ...)``. The
local-only tool keys leave the server's instance page and registry view.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

from spark import mode
from spark.instance_settings import access, accessor, audit, registry
from spark.instance_settings.registry import Scope
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.safety import untrusted
from spark.web.server import attach_database, create_app
from tests.web_helpers import sign_in

SOURCES = ("web", "mcp", "documents", "agents")
LOCAL_PREFIXES = (
    "embedded_tools.system_commands.",
    "embedded_tools.filesystem.",
    "embedded_tools.email.",
)


class MemorySecrets:
    def __init__(self) -> None:
        self.data: dict[str, str] = {"embedded_tools_email_password": "smtp-dev-only"}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self.data

    def resolve_uri(self, value: str) -> str | None:
        return value


class Ctx:
    def __init__(self, config: Path) -> None:
        config.write_text("{}\n", encoding="utf-8", newline="\n")
        self.settings = Settings(
            config_file=config, defaults={"interface": {"session_timeout_minutes": 60}}
        )
        self.secrets = MemorySecrets()


class Deny:
    def is_administrator(self, request: Any) -> bool:
        return False

    def user_guid(self, request: Any) -> str:
        return "user-not-admin"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()
    accessor.reset()
    access.reset()
    audit.set_audit_hook(None)
    untrusted.set_audit_hook(None)


@pytest.fixture
def events() -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    def hook(event: str, details: Mapping[str, Any]) -> None:
        recorded.append((event, dict(details)))

    untrusted.set_audit_hook(hook)
    return recorded


def _policy_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [d for e, d in events if e == "tool_result.policy_changed"]


@pytest.fixture
def server(tmp_path: Path, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import spark.core.application as app_mod

    db = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    mode.configure(RuntimeSettings(Mode.SERVER, db))
    ctx = Ctx(tmp_path / "config.yaml")
    monkeypatch.setattr(app_mod, "_get_config_path", lambda: tmp_path / "config.yaml")
    app = create_app(ctx)  # type: ignore[arg-type]
    attach_database(app, db, True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as client:
        sign_in(client)
        yield client


def _rows(client: TestClient) -> dict[str, Any]:
    sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
    return {
        r["key"]: r["value"] for r in sync_db.fetch_all("SELECT key, value FROM instance_settings")
    }


# -- The registry view -------------------------------------------------------------------


class TestRegistryView:
    def test_server_view_leaves_out_the_local_only_tool_keys(self) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
        offered = registry.offered_keys(Scope.INSTANCE)
        assert not [k for k in offered if k.startswith(LOCAL_PREFIXES)]
        assert {f"embedded_tools.tool_results.{s}" for s in SOURCES} <= set(offered)
        assert "embedded_tools.web.enabled" in offered
        assert "embedded_tools.email.password" not in registry.offered_keys()

    def test_personal_view_is_the_whole_registry(self, tmp_path: Path) -> None:
        mode.configure(
            RuntimeSettings(
                Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db")
            )
        )
        assert registry.offered_keys(Scope.INSTANCE) == registry.keys(Scope.INSTANCE)

    def test_result_actions_are_instance_scope(self) -> None:
        for source in SOURCES:
            assert registry.spec(f"embedded_tools.tool_results.{source}").scope is Scope.INSTANCE


# -- The instance page and API ---------------------------------------------------------------


@pytest.mark.postgres
class TestResultPolicyOnAServer:
    def test_instance_page_shows_an_action_per_source(self, server: TestClient) -> None:
        html = server.get("/settings").text
        for source in SOURCES:
            assert f'data-key="embedded_tools.tool_results.{source}"' in html

    def test_instance_page_and_secret_view_have_no_local_tool_keys(
        self, server: TestClient
    ) -> None:
        html = server.get("/settings").text
        assert not re.search(r"embedded_tools\.(system_commands|filesystem|email)\.", html)
        status = server.get("/settings/api/secrets").json()
        assert "embedded_tools.email.password" not in status
        assert "providers.anthropic.api_key" in status

    def test_administrator_saves_the_policy_and_each_change_is_audited(
        self, server: TestClient, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        resp = server.post(
            "/settings/api/instance",
            json={
                "embedded_tools.tool_results.web": "withhold",
                "embedded_tools.tool_results.mcp": "mark",
                "embedded_tools.tool_results.documents": "warn",
            },
        )
        assert resp.status_code == 200, resp.text
        rows = _rows(server)
        assert rows["embedded_tools.tool_results.web"] == '"withhold"'
        assert rows["embedded_tools.tool_results.mcp"] == '"mark"'
        changed = sorted(_policy_events(events), key=lambda d: d["source"])
        user = str(server.app.state.user_guid)  # type: ignore[attr-defined]
        assert changed == [
            {
                "key": "embedded_tools.tool_results.mcp",
                "source": "mcp",
                "action": "mark",
                "previous": "warn",
                "actor": user,
            },
            {
                "key": "embedded_tools.tool_results.web",
                "source": "web",
                "action": "withhold",
                "previous": "warn",
                "actor": user,
            },
        ]

    def test_saving_the_same_action_again_is_not_a_change(
        self, server: TestClient, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        payload = {"embedded_tools.tool_results.agents": "withhold"}
        assert server.post("/settings/api/instance", json=payload).status_code == 200
        assert server.post("/settings/api/instance", json=payload).status_code == 200
        assert len(_policy_events(events)) == 1

    def test_other_settings_are_not_policy_events(
        self, server: TestClient, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        resp = server.post("/settings/api/instance", json={"embedded_tools.web.enabled": False})
        assert resp.status_code == 200
        assert _policy_events(events) == []

    def test_the_policy_reaches_the_turn(self, server: TestClient) -> None:
        from spark.instance_settings import consumers

        server.post("/settings/api/instance", json={"embedded_tools.tool_results.web": "withhold"})
        source = server.app.state.instance_settings  # type: ignore[attr-defined]
        config = consumers.embedded_tools_config(source, _NoSecrets())
        policy = untrusted.ResultPolicy.from_config(config)
        assert policy.action_for(untrusted.SourceCategory.WEB) is untrusted.ResultAction.WITHHOLD

    def test_non_administrator_refused_nothing_written_nothing_audited(
        self, server: TestClient, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        access.set_access_check(Deny())
        resp = server.post(
            "/settings/api/instance", json={"embedded_tools.tool_results.web": "mark"}
        )
        assert resp.status_code == 403
        assert _rows(server) == {}
        assert _policy_events(events) == []

    def test_refused_as_a_preference(
        self, server: TestClient, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        resp = server.post(
            "/settings/api/preferences", json={"embedded_tools.tool_results.web": "mark"}
        )
        assert resp.status_code == 400
        assert _policy_events(events) == []

    def test_refused_on_the_personal_endpoint(
        self, server: TestClient, tmp_path: Path, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        resp = server.post("/settings/api/save", json={"embedded_tools.tool_results.web": "mark"})
        assert resp.status_code == 409
        assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) == {}
        assert _rows(server) == {}
        assert _policy_events(events) == []

    def test_an_invalid_action_refused_and_nothing_written(
        self, server: TestClient, events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        resp = server.post(
            "/settings/api/instance",
            json={
                "embedded_tools.tool_results.web": "mark",
                "embedded_tools.tool_results.mcp": "x",
            },
        )
        assert resp.status_code == 400
        assert _rows(server) == {}
        assert _policy_events(events) == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"embedded_tools.system_commands.enabled": True},
            {"embedded_tools.email.password": "smtp-new-dev-only"},
            {"embedded_tools": {"filesystem": {"enabled": True}}},
        ],
    )
    def test_local_tool_refusal_kept(self, server: TestClient, payload: dict[str, Any]) -> None:
        resp = server.post("/settings/api/instance", json=payload)
        assert resp.status_code == 403
        assert _rows(server) == {}


class _NoSecrets:
    def resolve(self, key: str) -> str:
        return ""
