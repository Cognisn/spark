"""Boolean settings read the same from config.yaml and from SPARK__ variables.

konfig returns an environment override as the string it was given, so
``SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=false`` reads as ``'false'``,
which a plain truthiness test treats as on. These tests load real konfig
settings with monkeypatched ``SPARK__`` variables and check every boolean read
goes through :func:`spark.settings_values.setting_bool`. The browser heartbeat
never runs in server mode, whatever the setting says.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from konfig.settings.settings import Settings

from spark import mode
from spark.core.application import _default_settings
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.settings_values import SettingValueError, coerce_bool, setting_bool

TRUE_SPELLINGS = ["true", "True", "TRUE", " true ", "1", "yes", "YES", "on", " On\n"]
FALSE_SPELLINGS = ["false", "False", "FALSE", " false ", "0", "no", "No", "off", "OFF "]


@pytest.fixture(autouse=True)
def _reset_mode() -> Iterator[None]:
    mode.reset()
    yield
    mode.reset()


def _konfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """konfig's settings as SPARK loads them, with these SPARK__ overrides."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8", newline="\n")
    return Settings(config_file=config, defaults=_default_settings(), env_prefix="SPARK")


def _ctx(settings: Any) -> SimpleNamespace:
    return SimpleNamespace(settings=settings)


def _server() -> None:
    mode.configure(
        RuntimeSettings(
            Mode.SERVER,
            DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://db.example.com/spark"),
        )
    )


def _personal(tmp_path: Path) -> None:
    mode.configure(
        RuntimeSettings(
            Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
        )
    )


class TestAccessor:
    @pytest.mark.parametrize("value", TRUE_SPELLINGS)
    def test_true_spellings(self, value: str) -> None:
        assert coerce_bool(value, "x.enabled", False) is True

    @pytest.mark.parametrize("value", FALSE_SPELLINGS)
    def test_false_spellings(self, value: str) -> None:
        assert coerce_bool(value, "x.enabled", True) is False

    def test_real_booleans_and_integers(self) -> None:
        assert coerce_bool(True, "k", False) is True
        assert coerce_bool(False, "k", True) is False
        assert coerce_bool(1, "k", False) is True
        assert coerce_bool(0, "k", True) is False

    def test_missing_value_takes_the_default(self) -> None:
        assert coerce_bool(None, "k", True) is True
        assert coerce_bool(None, "k", False) is False

    @pytest.mark.parametrize("value", ["", "  ", "maybe", "flase", "2", 2, -1, 0.0, [], {}])
    def test_anything_else_is_refused_naming_the_key(self, value: object) -> None:
        with pytest.raises(SettingValueError) as caught:
            coerce_bool(value, "interface.browser_heartbeat.enabled", True)
        assert caught.value.key == "interface.browser_heartbeat.enabled"
        assert "interface.browser_heartbeat.enabled" in str(caught.value)

    def test_refusal_is_a_value_error(self) -> None:
        with pytest.raises(ValueError):
            coerce_bool("nope", "k", True)

    @pytest.mark.parametrize("value", FALSE_SPELLINGS)
    def test_reads_each_false_spelling_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        settings = _konfig(tmp_path, monkeypatch, SPARK__INTERFACE__OPEN_BROWSER=value)
        assert settings.get("interface.open_browser", True) == value  # konfig gives the string
        assert setting_bool(settings, "interface.open_browser", True) is False

    @pytest.mark.parametrize("value", TRUE_SPELLINGS)
    def test_reads_each_true_spelling_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        settings = _konfig(tmp_path, monkeypatch, SPARK__AUTONOMOUS_ACTIONS__ENABLED=value)
        assert setting_bool(settings, "autonomous_actions.enabled", False) is True

    def test_default_from_the_defaults_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SPARK__INTERFACE__OPEN_BROWSER", raising=False)
        settings = _konfig(tmp_path, monkeypatch)
        assert setting_bool(settings, "interface.open_browser", False) is True


class TestBrowserHeartbeat:
    """The defect behind wave 6 Q34: the heartbeat ran in server mode and stopped the web."""

    def test_off_in_server_mode_with_the_dockerfile_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.web.server import _heartbeat_config

        _server()
        settings = _konfig(
            tmp_path, monkeypatch, SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED="false"
        )
        assert _heartbeat_config(_ctx(settings)) is None

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
    def test_off_in_server_mode_whatever_the_setting_says(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        from spark.web.server import _heartbeat_config

        _server()
        settings = _konfig(
            tmp_path, monkeypatch, SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=value
        )
        assert _heartbeat_config(_ctx(settings)) is None

    def test_off_in_server_mode_with_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.web.server import _heartbeat_config

        _server()
        monkeypatch.delenv("SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED", raising=False)
        assert _heartbeat_config(_ctx(_konfig(tmp_path, monkeypatch))) is None

    @pytest.mark.parametrize("value", FALSE_SPELLINGS)
    def test_off_in_personal_mode_when_the_environment_says_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        from spark.web.server import _heartbeat_config

        _personal(tmp_path)
        settings = _konfig(
            tmp_path, monkeypatch, SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=value
        )
        assert _heartbeat_config(_ctx(settings)) is None

    def test_on_in_personal_mode_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.web.server import _heartbeat_config

        _personal(tmp_path)
        monkeypatch.delenv("SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED", raising=False)
        settings = _konfig(
            tmp_path,
            monkeypatch,
            SPARK__INTERFACE__BROWSER_HEARTBEAT__INTERVAL_SECONDS="45",
            SPARK__INTERFACE__BROWSER_HEARTBEAT__MISS_THRESHOLD="4",
        )
        assert _heartbeat_config(_ctx(settings)) == (45, 4)

    def test_a_process_without_a_mode_is_personal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.web.server import _heartbeat_config

        settings = _konfig(tmp_path, monkeypatch, SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED="on")
        assert _heartbeat_config(_ctx(settings)) == (30, 3)

    def test_a_misspelt_setting_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.web.server import _heartbeat_config

        _personal(tmp_path)
        settings = _konfig(
            tmp_path, monkeypatch, SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED="flase"
        )
        with pytest.raises(SettingValueError, match="interface.browser_heartbeat.enabled"):
            _heartbeat_config(_ctx(settings))


class TestOtherBooleanReads:
    """Every other boolean read from konfig follows the environment's spelling."""

    def test_open_browser(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark.web.server import _should_open_browser

        settings = _konfig(tmp_path, monkeypatch, SPARK__INTERFACE__OPEN_BROWSER="off")
        assert _should_open_browser(_ctx(settings)) is False

    def test_ssl_scheme(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark.web.server import _initial_origin_policy

        settings = _konfig(
            tmp_path,
            monkeypatch,
            SPARK__INTERFACE__SSL__ENABLED="false",
            SPARK__INTERFACE__PORT="22500",
        )
        policy = _initial_origin_policy(_ctx(settings))
        assert "http://127.0.0.1:22500" in policy.allowed
        assert not any(origin.startswith("https://") for origin in policy.allowed)

    def test_providers_switched_off_in_the_environment_are_not_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.llm.factory import build_providers
        from spark.web.server import _build_providers

        settings = _konfig(
            tmp_path,
            monkeypatch,
            SPARK__PROVIDERS__ANTHROPIC__ENABLED="false",
            SPARK__PROVIDERS__AWS_BEDROCK__ENABLED="false",
            SPARK__PROVIDERS__OLLAMA__ENABLED="false",
            SPARK__PROVIDERS__GOOGLE_GEMINI__ENABLED="false",
            SPARK__PROVIDERS__XAI__ENABLED="false",
        )
        assert _build_providers(_ctx(settings), settings) == []  # type: ignore[arg-type]
        assert build_providers(settings) == []

    def test_actions_enabled_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark.web.endpoints.actions import _actions_enabled

        settings = _konfig(tmp_path, monkeypatch, SPARK__AUTONOMOUS_ACTIONS__ENABLED="false")
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=_ctx(settings))))
        assert _actions_enabled(request) is False  # type: ignore[arg-type]

    def test_settings_page_toggle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark.web.endpoints.settings import _toggle

        settings = _konfig(tmp_path, monkeypatch, SPARK__DAEMON__ENABLED="false")
        assert _toggle("daemon.enabled", "Daemon", settings)["value"] is False

    def test_voice_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark.voice.engine import load_config

        settings = _konfig(tmp_path, monkeypatch, SPARK__VOICE__ELEVENLABS__CACHE_ENABLED="no")
        assert load_config(SimpleNamespace(settings=settings, secrets=None)).cache_enabled is False

    def test_settings_page_select(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark.web.endpoints.settings import _select_bool

        settings = _konfig(tmp_path, monkeypatch, SPARK__EMBEDDED_TOOLS__EMAIL__USE_TLS="No")
        assert _select_bool(settings, "embedded_tools.email.use_tls", True) == "false"
