"""Voice settings registration, plus the merged bool-key regression."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from spark.instance_settings import registry
from spark.instance_settings.registry import Kind
from spark.voice.engine import load_config


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, cast: Any = None) -> Any:
        value = self.values.get(key, default)
        return cast(value) if cast is not None and value is not None else value


class _Secrets:
    def __init__(self, data: dict[str, str]) -> None:
        self.data = data

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def resolve_uri(self, value: str) -> str | None:
        return self.data.get(value.removeprefix("secret://"))


class TestVoiceSecret:
    def test_api_key_is_a_secret(self) -> None:
        assert registry.spec("voice.elevenlabs.api_key").secret

    def test_secret_name_matches_what_the_engine_reads(self) -> None:
        # load_config() reads ctx.secrets.get("voice_elevenlabs_api_key"); if this
        # derivation ever drifts, the key silently stops being found.
        assert registry.secret_name("voice.elevenlabs.api_key") == "voice_elevenlabs_api_key"
        ctx = SimpleNamespace(
            settings=_Settings({}), secrets=_Secrets({"voice_elevenlabs_api_key": "el-dev-only"})
        )
        assert load_config(ctx).api_key == "el-dev-only"

    def test_the_0_2_name_is_still_read(self) -> None:
        ctx = SimpleNamespace(
            settings=_Settings({}), secrets=_Secrets({"elevenlabs_api_key": "el-dev-only"})
        )
        assert load_config(ctx).api_key == "el-dev-only"

    def test_a_reference_in_the_config_is_resolved_not_used_as_the_key(self) -> None:
        ctx = SimpleNamespace(
            settings=_Settings({"voice.elevenlabs.api_key": "secret://elsewhere"}),
            secrets=_Secrets({"elsewhere": "el-dev-only"}),
        )
        assert load_config(ctx).api_key == "el-dev-only"


class TestIntKeys:
    def test_numeric_voice_settings_are_ints(self) -> None:
        assert registry.spec("voice.elevenlabs.monthly_character_cap").kind is Kind.INT
        assert registry.spec("voice.elevenlabs.cache_max_mb").kind is Kind.INT


class TestBoolKeysRegression:
    def test_email_bool_keys_survive(self) -> None:
        assert registry.spec("embedded_tools.email.use_tls").kind is Kind.BOOL
        assert registry.spec("embedded_tools.email.require_approval").kind is Kind.BOOL

    def test_system_command_bool_key_survives(self) -> None:
        assert registry.spec("embedded_tools.system_commands.require_approval").kind is Kind.BOOL

    def test_voice_bool_key_registered(self) -> None:
        assert registry.spec("voice.elevenlabs.cache_enabled").kind is Kind.BOOL
        assert registry.coerce("voice.elevenlabs.cache_enabled", "false") is False
