"""The settings pages and API in personal and server mode.

Guards under test: secrets never returned to the browser, secrets never written
to the database, administrator-only settings refused for a non-administrator,
unknown keys refused (and nothing written when any key in a request is refused).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

from spark import mode
from spark.instance_settings import access, accessor, audit, registry
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.web.server import attach_database, create_app
from tests.db_helpers import app_client

KEY = "sk-ant-web-test-dev-only"
OTHER = "brave-web-test-dev-only"


class MemorySecrets:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

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

    def list_keys(self) -> list[str]:
        return list(self.data)


class Ctx:
    def __init__(self, config: Path, secrets: MemorySecrets) -> None:
        self.settings = Settings(
            config_file=config,
            defaults={"interface": {"session_timeout_minutes": 60}},
        )
        self.secrets = secrets


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


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "enabled": True,
                        "api_key": "secret://providers_anthropic_api_key",
                    }
                }
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    return path


@pytest.fixture
def store() -> MemorySecrets:
    return MemorySecrets({"providers_anthropic_api_key": KEY})


def _signed_in(client: TestClient) -> TestClient:
    code = client.app.state.auth.generate_code()  # type: ignore[attr-defined]
    resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
    client.cookies.set("spark_session", resp.cookies["spark_session"])
    return client


def _page_keys(html: str) -> set[str]:
    import re

    # Form controls only (the page's scripts also mention data-key selectors).
    return set(re.findall(r'<(?:input|select|textarea|div)\s[^>]*?data-key="([^"$]+)"', html))


def _no_secret(resp: Any, *values: str) -> None:
    for value in values or (KEY, OTHER):
        assert value not in resp.text


# -- Personal mode ----------------------------------------------------------------------


@pytest.fixture
def personal(config_file: Path, store: MemorySecrets, sqlite_settings: Any) -> Iterator[Any]:
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    app = create_app(Ctx(config_file, store))  # type: ignore[arg-type]
    with app_client(app, sqlite_settings) as client:
        yield _signed_in(client)


class TestPersonalPage:
    def test_one_page_as_today_with_every_section(self, personal: TestClient) -> None:
        resp = personal.get("/settings")
        assert resp.status_code == 200
        for title in ("LLM Providers", "Database", "Global Instructions", "Embedded Tools"):
            assert title in resp.text
        _no_secret(resp)

    def test_a_secret_shows_as_set_never_its_value(self, personal: TestClient) -> None:
        resp = personal.get("/settings")
        assert 'data-secret-set="true"' in resp.text
        _no_secret(resp)

    def test_every_field_on_the_page_is_registered(self, personal: TestClient) -> None:
        """Saving the page must never be refused for a key the page itself sent."""
        keys = _page_keys(personal.get("/settings").text)
        assert keys
        assert [k for k in keys if not registry.is_known(k)] == []

    def test_reveal_endpoint_is_gone(self, personal: TestClient) -> None:
        resp = personal.get("/settings/api/reveal-secret?key=providers.anthropic.api_key")
        assert resp.status_code == 404
        _no_secret(resp)

    def test_preferences_page_redirects_to_the_one_page(self, personal: TestClient) -> None:
        resp = personal.get("/settings/preferences", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings"

    def test_server_endpoints_refused(self, personal: TestClient) -> None:
        assert personal.post("/settings/api/instance", json={}).status_code == 409
        assert personal.post("/settings/api/preferences", json={}).status_code == 409


class TestPersonalSave:
    def test_secret_goes_to_the_keychain_and_the_config_keeps_a_reference(
        self, personal: TestClient, config_file: Path, store: MemorySecrets
    ) -> None:
        resp = personal.post("/settings/api/save", json={"embedded_tools.web.brave_api_key": OTHER})
        assert resp.status_code == 200
        _no_secret(resp)
        assert store.data["embedded_tools_web_brave_api_key"] == OTHER
        text = config_file.read_text(encoding="utf-8")
        assert OTHER not in text
        saved = yaml.safe_load(text)
        assert saved["embedded_tools"]["web"]["brave_api_key"] == (
            "secret://embedded_tools_web_brave_api_key"
        )

    def test_database_url_saved_as_database_url(
        self, personal: TestClient, store: MemorySecrets, config_file: Path
    ) -> None:
        url = "postgresql://spark:pw-web-dev-only@db.example.com/spark"
        resp = personal.post("/settings/api/save", json={"database.url": url})
        assert resp.status_code == 200
        assert store.data["database_url"] == url
        assert "url" not in store.data
        assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["database"]["url"] == (
            "secret://database_url"
        )

    def test_an_empty_secret_leaves_the_stored_one(
        self, personal: TestClient, store: MemorySecrets
    ) -> None:
        resp = personal.post("/settings/api/save", json={"providers.anthropic.api_key": ""})
        assert resp.status_code == 200
        assert store.data["providers_anthropic_api_key"] == KEY

    def test_values_are_coerced(self, personal: TestClient, config_file: Path) -> None:
        resp = personal.post(
            "/settings/api/save",
            json={
                "voice.elevenlabs.monthly_character_cap": "500",
                "embedded_tools.email.use_tls": "false",
                "embedded_tools.system_commands.blocked_commands": "rm, reboot",
            },
        )
        assert resp.status_code == 200
        saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert saved["voice"]["elevenlabs"]["monthly_character_cap"] == 500
        assert saved["embedded_tools"]["email"]["use_tls"] is False
        assert saved["embedded_tools"]["system_commands"]["blocked_commands"] == ["rm", "reboot"]

    def test_unknown_keys_refused_and_nothing_written(
        self, personal: TestClient, config_file: Path
    ) -> None:
        before = config_file.read_text(encoding="utf-8")
        resp = personal.post(
            "/settings/api/save", json={"voice.engine": "elevenlabs", "database.type": "mysql"}
        )
        assert resp.status_code == 400
        assert resp.json()["keys"] == ["database.type"]
        assert config_file.read_text(encoding="utf-8") == before

    def test_invalid_value_refused_and_nothing_written(
        self, personal: TestClient, config_file: Path, store: MemorySecrets
    ) -> None:
        before = config_file.read_text(encoding="utf-8")
        resp = personal.post(
            "/settings/api/save",
            json={"embedded_tools.web.brave_api_key": OTHER, "voice.engine": "telegraph"},
        )
        assert resp.status_code == 400
        assert config_file.read_text(encoding="utf-8") == before
        assert "embedded_tools_web_brave_api_key" not in store.data
        _no_secret(resp)

    def test_secret_status(self, personal: TestClient) -> None:
        resp = personal.get("/settings/api/secrets")
        assert resp.status_code == 200
        status = resp.json()
        assert status["providers.anthropic.api_key"] is True
        assert status["providers.xai.api_key"] is False
        _no_secret(resp)

    def test_clear_secret(self, personal: TestClient, store: MemorySecrets) -> None:
        resp = personal.post(
            "/settings/api/secrets/clear", json={"key": "providers.anthropic.api_key"}
        )
        assert resp.status_code == 200
        assert "providers_anthropic_api_key" not in store.data

    def test_clear_refuses_a_non_secret_key(self, personal: TestClient) -> None:
        resp = personal.post("/settings/api/secrets/clear", json={"key": "voice.engine"})
        assert resp.status_code == 400


# -- Server mode --------------------------------------------------------------------------


@pytest.fixture
def server_db(pg_url: str) -> DatabaseSettings:
    return DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)


@pytest.fixture
def server(config_file: Path, store: MemorySecrets, server_db: DatabaseSettings) -> Iterator[Any]:
    mode.configure(RuntimeSettings(Mode.SERVER, server_db))
    app = create_app(Ctx(config_file, store))  # type: ignore[arg-type]
    attach_database(app, server_db, True)
    # psycopg's async driver needs the Selector loop on Windows; the portal's loop too.
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as client:
        yield _signed_in(client)


def _rows(client: TestClient) -> list[str]:
    """Every value in SPARK's settings tables, as text."""
    sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
    rows = sync_db.fetch_all("SELECT key, value, updated_by FROM instance_settings")
    rows += sync_db.fetch_all("SELECT user_guid, key, value FROM user_preferences")
    return [str(v) for row in rows for v in row.values()]


@pytest.mark.postgres
class TestServerPages:
    def test_administrator_gets_the_instance_page(self, server: TestClient) -> None:
        resp = server.get("/settings")
        assert resp.status_code == 200
        assert "Instance settings" in resp.text
        assert "LLM Providers" in resp.text
        assert 'data-key="conversation.global_instructions"' not in resp.text  # a preference
        assert 'data-key="mode"' not in resp.text  # start-up configuration, not editable here
        _no_secret(resp)

    def test_instance_page_fields_are_instance_settings(self, server: TestClient) -> None:
        keys = _page_keys(server.get("/settings").text)
        assert keys
        assert {registry.spec(k).scope for k in keys} == {registry.Scope.INSTANCE}

    def test_preferences_page_fields_are_preferences(self, server: TestClient) -> None:
        keys = _page_keys(server.get("/settings/preferences").text)
        assert {registry.spec(k).scope for k in keys} == {registry.Scope.USER}

    def test_non_administrator_is_sent_to_preferences(self, server: TestClient) -> None:
        access.set_access_check(Deny())
        resp = server.get("/settings", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings/preferences"

    def test_preferences_page(self, server: TestClient) -> None:
        access.set_access_check(Deny())
        resp = server.get("/settings/preferences")
        assert resp.status_code == 200
        assert 'data-key="conversation.global_instructions"' in resp.text
        assert 'data-key="default_model.model_id"' in resp.text
        assert 'data-key="interface.theme"' in resp.text
        assert "LLM Providers" not in resp.text
        _no_secret(resp)

    def test_personal_save_endpoint_refused(self, server: TestClient, config_file: Path) -> None:
        before = config_file.read_text(encoding="utf-8")
        resp = server.post("/settings/api/save", json={"voice.engine": "elevenlabs"})
        assert resp.status_code == 409
        assert config_file.read_text(encoding="utf-8") == before


@pytest.mark.postgres
class TestServerInstanceSave:
    def test_plain_settings_go_to_the_database(self, server: TestClient) -> None:
        resp = server.post(
            "/settings/api/instance",
            json={"voice.engine": "elevenlabs", "models.offered": "m-1, m-2"},
        )
        assert resp.status_code == 200
        assert accessor.get("voice.engine") == "elevenlabs"
        assert accessor.get("models.offered") == ["m-1", "m-2"]
        assert '"elevenlabs"' in _rows(server)

    def test_secrets_go_to_konfig_and_never_to_the_database(
        self, server: TestClient, store: MemorySecrets
    ) -> None:
        resp = server.post(
            "/settings/api/instance",
            json={"embedded_tools.web.brave_api_key": OTHER, "voice.engine": "elevenlabs"},
        )
        assert resp.status_code == 200
        _no_secret(resp)
        assert store.data["embedded_tools_web_brave_api_key"] == OTHER
        assert all(OTHER not in value for value in _rows(server))
        assert all(KEY not in value for value in _rows(server))

    def test_non_administrator_refused_and_nothing_written(
        self, server: TestClient, store: MemorySecrets
    ) -> None:
        access.set_access_check(Deny())
        resp = server.post(
            "/settings/api/instance",
            json={"voice.engine": "elevenlabs", "embedded_tools.web.brave_api_key": OTHER},
        )
        assert resp.status_code == 403
        assert _rows(server) == []
        assert "embedded_tools_web_brave_api_key" not in store.data

    def test_unknown_key_refused_and_nothing_written(self, server: TestClient) -> None:
        resp = server.post(
            "/settings/api/instance", json={"voice.engine": "elevenlabs", "nope.nope": 1}
        )
        assert resp.status_code == 400
        assert resp.json()["keys"] == ["nope.nope"]
        assert _rows(server) == []

    def test_preference_and_startup_keys_refused_on_the_instance_endpoint(
        self, server: TestClient
    ) -> None:
        for payload in ({"interface.theme": "light"}, {"mode": "personal"}):
            resp = server.post("/settings/api/instance", json=payload)
            assert resp.status_code == 400
        assert _rows(server) == []

    def test_secret_status_and_clear_are_administrator_only(
        self, server: TestClient, store: MemorySecrets
    ) -> None:
        resp = server.get("/settings/api/secrets")
        assert resp.status_code == 200
        assert resp.json()["providers.anthropic.api_key"] is True
        _no_secret(resp)
        access.set_access_check(Deny())
        assert server.get("/settings/api/secrets").status_code == 403
        resp = server.post(
            "/settings/api/secrets/clear", json={"key": "providers.anthropic.api_key"}
        )
        assert resp.status_code == 403
        assert store.data["providers_anthropic_api_key"] == KEY


@pytest.mark.postgres
class TestServerPreferences:
    def test_any_user_saves_their_own_preferences(self, server: TestClient) -> None:
        access.set_access_check(Deny())
        resp = server.post(
            "/settings/api/preferences",
            json={"interface.theme": "light", "conversation.global_instructions": "Be brief."},
        )
        assert resp.status_code == 200
        rows = server.app.state.sync_db.fetch_all(  # type: ignore[attr-defined]
            "SELECT user_guid, key FROM user_preferences ORDER BY key"
        )
        assert [(r["user_guid"], r["key"]) for r in rows] == [
            ("user-not-admin", "conversation.global_instructions"),
            ("user-not-admin", "interface.theme"),
        ]

    def test_instance_and_secret_keys_refused(self, server: TestClient) -> None:
        for payload in (
            {"voice.engine": "elevenlabs"},
            {"providers.anthropic.api_key": KEY},
            {"nope.nope": 1},
        ):
            resp = server.post("/settings/api/preferences", json=payload)
            assert resp.status_code == 400
            _no_secret(resp)
        assert _rows(server) == []
