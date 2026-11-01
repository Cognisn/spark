"""The web process in each mode: the tray daemon gate and the settings listener lifespan.

Guards under test: a server-mode web process never starts the tray daemon; the
lifespan starts the instance settings listener in server mode and stops it (its
connection closed) on shutdown; a change saved on one web instance is read on a
second, genuinely separate instance (its own app, pool and listener) within the
propagation bound.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

import spark.web.server as server
from spark import mode
from spark.instance_settings import FileSettings, ServerSettings, access, accessor, audit
from spark.instance_settings.listener import APPLICATION_NAME, SettingsListener
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.web.server import attach_database, create_app
from tests.web_helpers import sign_in

BOUND_SECONDS = 5.0


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
        if value.startswith("secret://"):
            return self.data.get(value[len("secret://") :])
        return value


class Ctx:
    def __init__(self, config: Path, values: dict[str, Any] | None = None) -> None:
        config.write_text(yaml.safe_dump(values or {}), encoding="utf-8", newline="\n")
        self.settings = Settings(
            config_file=config,
            defaults={"interface": {"session_timeout_minutes": 60}},
        )
        self.secrets = MemorySecrets()


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()
    accessor.reset()
    access.reset()
    audit.set_audit_hook(None)


def _client_options() -> dict[str, Any]:
    # psycopg's async driver needs the Selector loop on Windows; the portal's loop too.
    return {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


@contextmanager
def _server_app(tmp_path: Path, name: str, db: DatabaseSettings) -> Iterator[TestClient]:
    app = create_app(Ctx(tmp_path / f"{name}.yaml"))  # type: ignore[arg-type]
    attach_database(app, db, True)
    with TestClient(app, backend_options=_client_options()) as client:
        sign_in(client)
        yield client


def _listener_connections(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
            (APPLICATION_NAME,),
        ).fetchone()
    return int(row[0]) if row else 0


def _wait_for(predicate: Any, timeout: float = BOUND_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


# -- The tray daemon gate ---------------------------------------------------------------


class _DaemonCtx:
    class settings:  # noqa: N801 - mimics konfig's attribute
        @staticmethod
        def get(key: str, default: Any = None, **_: Any) -> Any:
            return {"autonomous_actions.enabled": True, "daemon.enabled": True}.get(key, default)


class TestTrayDaemonGate:
    @pytest.fixture
    def started(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []
        monkeypatch.setattr(server, "_start_tray_daemon_if_needed", lambda: calls.append("tray"))
        return calls

    def test_server_mode_never_starts_the_tray_daemon(
        self, started: list[str], tmp_path: Path
    ) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
        status: dict[str, Any] = {}
        server._start_daemon_if_enabled(_DaemonCtx(), status)  # type: ignore[arg-type]
        assert started == []

    def test_personal_mode_still_starts_it_when_enabled(
        self, started: list[str], tmp_path: Path
    ) -> None:
        mode.configure(
            RuntimeSettings(
                Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db")
            )
        )
        status: dict[str, Any] = {}
        server._start_daemon_if_enabled(_DaemonCtx(), status)  # type: ignore[arg-type]
        assert started == ["tray"]

    def test_personal_mode_leaves_it_off_when_disabled(
        self, started: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mode.configure(
            RuntimeSettings(
                Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db")
            )
        )
        monkeypatch.setattr(_DaemonCtx.settings, "get", staticmethod(lambda k, d=None, **_: d))
        server._start_daemon_if_enabled(_DaemonCtx(), {})  # type: ignore[arg-type]
        assert started == []


# -- The settings listener in the lifespan -------------------------------------------------


class TestPersonalLifespan:
    def test_file_settings_installed_and_no_listener(
        self, tmp_path: Path, sqlite_settings: Any
    ) -> None:
        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        app: FastAPI = create_app(Ctx(tmp_path / "c.yaml"))  # type: ignore[arg-type]
        attach_database(app, sqlite_settings, True)
        with TestClient(app):
            assert isinstance(app.state.instance_settings, FileSettings)
            assert app.state.settings_listener is None
            assert accessor.current() is app.state.instance_settings


@pytest.fixture
def server_db(pg_url: str) -> DatabaseSettings:
    db = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    mode.configure(RuntimeSettings(Mode.SERVER, db))
    return db


@pytest.mark.postgres
class TestServerLifespan:
    def test_listener_started_by_the_lifespan_and_stopped_on_shutdown(
        self, tmp_path: Path, server_db: DatabaseSettings, pg_url: str
    ) -> None:
        with _server_app(tmp_path, "a", server_db) as client:
            app = client.app
            listener = app.state.settings_listener  # type: ignore[attr-defined]
            assert isinstance(listener, SettingsListener)
            assert listener.connected.is_set()
            assert isinstance(app.state.instance_settings, ServerSettings)  # type: ignore[attr-defined]
            assert _listener_connections(pg_url) == 1
        assert app.state.settings_listener is None  # type: ignore[attr-defined]
        assert not listener.connected.is_set()
        assert _wait_for(lambda: _listener_connections(pg_url) == 0)

    def test_a_change_on_one_instance_is_read_on_another_within_the_bound(
        self, tmp_path: Path, server_db: DatabaseSettings
    ) -> None:
        with ExitStack() as stack:
            a = stack.enter_context(_server_app(tmp_path, "a", server_db))
            b = stack.enter_context(_server_app(tmp_path, "b", server_db))
            b_source = b.app.state.instance_settings  # type: ignore[attr-defined]
            assert b_source is not a.app.state.instance_settings  # type: ignore[attr-defined]
            assert b_source.get("conversation.max_tool_iterations") == 25

            resp = a.post("/settings/api/instance", json={"conversation.max_tool_iterations": 7})
            assert resp.status_code == 200, resp.text

            started = time.monotonic()
            assert _wait_for(lambda: b_source.get("conversation.max_tool_iterations") == 7)
            assert time.monotonic() - started < BOUND_SECONDS
            page = b.get("/settings")
            assert page.status_code == 200
            assert re.search(r'data-key="conversation\.max_tool_iterations"\s+value="7"', page.text)


# -- Settings consumers on a second instance ------------------------------------------------


class FakeProvider:
    def __init__(self, name: str, models: list[str]) -> None:
        self.name, self.models = name, models

    def get_provider_name(self) -> str:
        return self.name

    def list_available_models(self) -> list[dict[str, Any]]:
        return [{"id": m, "name": m} for m in self.models]


def _fake_providers(ctx: Any, view: Any) -> list[Any]:
    if view.get("providers.anthropic.enabled"):
        return [FakeProvider("anthropic", ["model-a", "model-b"])]
    return []


def _wired(client: TestClient) -> tuple[Any, Any]:
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver

    app = client.app
    instance = server._server_consumers(app, app.state.ctx)  # type: ignore[arg-type, attr-defined]
    assert instance is not None
    llm = server._init_providers(app.state.ctx, instance.view)  # type: ignore[attr-defined]
    conv = ConversationManager(
        app.state.sync_db,  # type: ignore[attr-defined]
        llm,
        ContextLimitResolver(None),
        embedded_tools_config=instance.embedded_tools_config(),
    )
    follower = instance.follow(llm, conv)
    follower.wait_idle()
    app.state.settings_follower = follower  # type: ignore[attr-defined]
    return llm, conv


class TestPersonalConsumers:
    def test_personal_mode_has_no_server_consumers(
        self, tmp_path: Path, sqlite_settings: Any
    ) -> None:
        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        app: FastAPI = create_app(Ctx(tmp_path / "c.yaml"))  # type: ignore[arg-type]
        attach_database(app, sqlite_settings, True)
        with TestClient(app):
            assert server._server_consumers(app, app.state.ctx) is None


@pytest.mark.postgres
class TestServerConsumers:
    def test_an_administrator_change_reaches_the_consumers_of_another_instance(
        self, tmp_path: Path, server_db: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(server, "_build_providers", _fake_providers)
        with ExitStack() as stack:
            a = stack.enter_context(_server_app(tmp_path, "a", server_db))
            b = stack.enter_context(_server_app(tmp_path, "b", server_db))
            llm, conv = _wired(b)
            assert llm.providers == {}
            assert conv._embedded_tools_config["embedded_tools"]["web"]["enabled"] is True

            resp = a.post(
                "/settings/api/instance",
                json={
                    "providers.anthropic.enabled": True,
                    "models.offered": "model-b",
                    "embedded_tools.web.enabled": False,
                    "conversation.max_tool_iterations": 4,
                },
            )
            assert resp.status_code == 200, resp.text

            assert _wait_for(
                lambda: "anthropic" in llm.providers
                and conv._embedded_tools_config["embedded_tools"]["web"]["enabled"] is False
                and conv._max_tool_iterations == 4
            )
            assert [m["id"] for m in llm.list_all_models()] == ["model-b"]
            assert "filesystem" not in conv._embedded_tools_config["embedded_tools"]

    def test_a_secret_saved_on_this_instance_rebuilds_its_providers(
        self, tmp_path: Path, server_db: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        keys: list[str] = []

        def builder(ctx: Any, view: Any) -> list[Any]:
            keys.append(view.get("providers.anthropic.api_key", ""))
            return _fake_providers(ctx, view)

        monkeypatch.setattr(server, "_build_providers", builder)
        with _server_app(tmp_path, "a", server_db) as a:
            _wired(a)
            resp = a.post(
                "/settings/api/instance", json={"providers.anthropic.api_key": "sk-wired-dev-only"}
            )
            assert resp.status_code == 200, resp.text
            assert _wait_for(lambda: keys[-1:] == ["sk-wired-dev-only"])


# -- The theme endpoint ----------------------------------------------------------------------


class TestPersonalTheme:
    def test_personal_mode_keeps_the_config_file(
        self, tmp_path: Path, sqlite_settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.core.application as app_mod

        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        ctx = Ctx(tmp_path / "c.yaml")
        monkeypatch.setattr(app_mod, "_get_config_path", lambda: tmp_path / "c.yaml")
        app: FastAPI = create_app(ctx)  # type: ignore[arg-type]
        attach_database(app, sqlite_settings, True)
        with TestClient(app) as client:
            sign_in(client)
            assert client.post("/api/theme", json={"theme": "light"}).status_code == 200
            assert client.get("/api/theme").json() == {"theme": "light"}
        saved = yaml.safe_load((tmp_path / "c.yaml").read_text(encoding="utf-8"))
        assert saved["interface"]["theme"] == "light"


@pytest.mark.postgres
class TestServerTheme:
    def test_theme_is_the_users_preference_not_the_config_file(
        self, tmp_path: Path, server_db: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.core.application as app_mod

        monkeypatch.setattr(app_mod, "_get_config_path", lambda: tmp_path / "a.yaml")
        with _server_app(tmp_path, "a", server_db) as client:
            before = (tmp_path / "a.yaml").read_bytes()
            assert client.get("/api/theme").json() == {"theme": "dark"}
            assert client.post("/api/theme", json={"theme": "light"}).status_code == 200
            assert client.get("/api/theme").json() == {"theme": "light"}
            assert (tmp_path / "a.yaml").read_bytes() == before
            user = client.app.state.user_guid  # type: ignore[attr-defined]
            rows = client.app.state.sync_db.fetch_all(  # type: ignore[attr-defined]
                "SELECT user_guid, key, value FROM user_preferences"
            )
            assert [(r["user_guid"], r["key"]) for r in rows] == [(user, "interface.theme")]

    def test_invalid_theme_refused(self, tmp_path: Path, server_db: DatabaseSettings) -> None:
        with _server_app(tmp_path, "a", server_db) as client:
            assert client.post("/api/theme", json={"theme": "neon"}).status_code == 400
