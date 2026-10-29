"""The typed registry of settings: keys, scopes, defaults and validation."""

from __future__ import annotations

import pytest

from spark.instance_settings import registry
from spark.instance_settings.registry import (
    InvalidSettingError,
    Scope,
    UnknownSettingError,
)


class TestLookup:
    def test_known_key_returns_its_spec(self) -> None:
        spec = registry.spec("providers.anthropic.enabled")
        assert spec.key == "providers.anthropic.enabled"
        assert spec.scope is Scope.INSTANCE
        assert spec.default is False

    def test_unknown_key_is_refused(self) -> None:
        with pytest.raises(UnknownSettingError):
            registry.spec("providers.anthropic.not_a_setting")

    def test_is_known(self) -> None:
        assert registry.is_known("mode")
        assert not registry.is_known("nothing.here")

    def test_keys_by_scope(self) -> None:
        user = registry.keys(Scope.USER)
        assert "default_model.model_id" in user
        assert "conversation.global_instructions" in user
        assert "interface.theme" in user
        assert "voice.interaction_mode" in user
        assert "providers.anthropic.enabled" not in user

    def test_a4_instance_settings_are_administrator_scope(self) -> None:
        for key in (
            "providers.anthropic.enabled",
            "models.offered",
            "mcp.servers",
            "tool_policy.actions",
            "embedded_tools.email.enabled",
            "voice.engine",
            "files.quota_mb",
            "files.max_upload_mb",
        ):
            assert registry.spec(key).scope is Scope.INSTANCE, key

    def test_startup_configuration_keys(self) -> None:
        for key in (
            "mode",
            "database.url",
            "server.public_base_url",
            "warden.url",
            "warden.client_id",
            "warden.client_secret",
        ):
            assert registry.spec(key).scope is Scope.STARTUP, key


class TestSecrets:
    def test_secret_keys_are_flagged(self) -> None:
        for key in (
            "providers.anthropic.api_key",
            "providers.aws_bedrock.secret_key",
            "database.url",
            "embedded_tools.email.password",
            "embedded_tools.web.brave_api_key",
            "voice.elevenlabs.api_key",
            "warden.client_secret",
        ):
            assert registry.spec(key).secret, key

    def test_plain_keys_are_not_secret(self) -> None:
        assert not registry.spec("providers.anthropic.enabled").secret
        assert not registry.spec("embedded_tools.email.host").secret

    def test_secret_keys_listing(self) -> None:
        secrets = registry.secret_keys()
        assert "database.url" in secrets
        assert "providers.anthropic.enabled" not in secrets

    def test_secret_name_includes_the_group(self) -> None:
        assert registry.secret_name("database.url") == "database_url"
        assert registry.secret_name("providers.anthropic.api_key") == "providers_anthropic_api_key"
        assert registry.secret_name("voice.elevenlabs.api_key") == "voice_elevenlabs_api_key"

    def test_legacy_secret_name_drops_the_group(self) -> None:
        assert registry.legacy_secret_name("database.url") == "url"
        assert registry.legacy_secret_name("voice.elevenlabs.api_key") == "elevenlabs_api_key"

    def test_secret_name_refuses_non_secret_keys(self) -> None:
        with pytest.raises(InvalidSettingError):
            registry.secret_name("providers.anthropic.enabled")

    def test_secret_names_are_unique(self) -> None:
        names = [registry.secret_name(k) for k in registry.secret_keys()]
        assert len(names) == len(set(names))


class TestCoerce:
    def test_bool_accepts_bool_and_form_strings(self) -> None:
        key = "providers.anthropic.enabled"
        assert registry.coerce(key, True) is True
        assert registry.coerce(key, "true") is True
        assert registry.coerce(key, "false") is False

    def test_bool_refuses_other_values(self) -> None:
        with pytest.raises(InvalidSettingError):
            registry.coerce("providers.anthropic.enabled", "maybe")
        with pytest.raises(InvalidSettingError):
            registry.coerce("providers.anthropic.enabled", 3)

    def test_int_accepts_numbers_and_numeric_strings(self) -> None:
        key = "voice.elevenlabs.monthly_character_cap"
        assert registry.coerce(key, 5) == 5
        assert registry.coerce(key, "7") == 7
        assert registry.coerce(key, 8.0) == 8

    def test_int_refuses_bool_fraction_and_text(self) -> None:
        key = "voice.elevenlabs.monthly_character_cap"
        for bad in (True, 1.5, "lots", None):
            with pytest.raises(InvalidSettingError):
                registry.coerce(key, bad)

    def test_int_bounds(self) -> None:
        key = "embedded_tools.system_commands.timeout"
        assert registry.coerce(key, 300) == 300
        with pytest.raises(InvalidSettingError):
            registry.coerce(key, 0)
        with pytest.raises(InvalidSettingError):
            registry.coerce(key, 301)

    def test_float_bounds(self) -> None:
        key = "conversation.rollup_threshold"
        assert registry.coerce(key, "0.5") == 0.5
        with pytest.raises(InvalidSettingError):
            registry.coerce(key, 1.5)

    def test_choice(self) -> None:
        assert registry.coerce("voice.engine", "elevenlabs") == "elevenlabs"
        with pytest.raises(InvalidSettingError):
            registry.coerce("voice.engine", "telegraph")

    def test_string_list_from_list_or_comma_string(self) -> None:
        key = "embedded_tools.system_commands.blocked_commands"
        assert registry.coerce(key, ["rm", " shutdown "]) == ["rm", "shutdown"]
        assert registry.coerce(key, "rm, reboot,,") == ["rm", "reboot"]
        with pytest.raises(InvalidSettingError):
            registry.coerce(key, [1, 2])

    def test_string_refuses_non_strings(self) -> None:
        with pytest.raises(InvalidSettingError):
            registry.coerce("embedded_tools.email.host", 42)

    def test_url_setting_requires_http_scheme(self) -> None:
        key = "server.public_base_url"
        assert registry.coerce(key, "https://spark.example.com") == "https://spark.example.com"
        assert registry.coerce(key, "") == ""
        for bad in ("spark.example.com", "ftp://spark.example.com", "https://"):
            with pytest.raises(InvalidSettingError):
                registry.coerce(key, bad)

    def test_mcp_servers_shape(self) -> None:
        good = [{"name": "docs", "transport": "http", "url": "https://mcp.example.com/mcp"}]
        assert registry.coerce("mcp.servers", good) == good
        for bad in (
            {"name": "docs"},
            [{"transport": "http"}],
            [{"name": "docs", "transport": "carrier-pigeon"}],
            [{"name": "a", "transport": "http"}, {"name": "a", "transport": "sse"}],
        ):
            with pytest.raises(InvalidSettingError):
                registry.coerce("mcp.servers", bad)

    def test_tool_policy_actions_shape(self) -> None:
        good = {"web": "withhold", "mcp": "warn"}
        assert registry.coerce("tool_policy.actions", good) == good
        for bad in ({"web": "ignore"}, ["web"], {"": "mark"}):
            with pytest.raises(InvalidSettingError):
                registry.coerce("tool_policy.actions", bad)

    def test_secret_values_must_be_non_empty_strings(self) -> None:
        assert registry.coerce("providers.anthropic.api_key", "sk-test-dev-only") == (
            "sk-test-dev-only"
        )
        with pytest.raises(InvalidSettingError):
            registry.coerce("providers.anthropic.api_key", 12)

    def test_unknown_key_refused_by_coerce(self) -> None:
        with pytest.raises(UnknownSettingError):
            registry.coerce("not.registered", 1)

    def test_error_messages_name_the_key_but_never_the_value(self) -> None:
        with pytest.raises(InvalidSettingError) as info:
            registry.coerce("providers.anthropic.api_key", 1234567)
        assert "providers.anthropic.api_key" in str(info.value)
        assert "1234567" not in str(info.value)


class TestDefaults:
    def test_every_default_validates(self) -> None:
        for key in registry.keys():
            spec = registry.spec(key)
            if spec.secret:
                continue
            assert registry.coerce(key, spec.default) == spec.default, key

    def test_defaults_match_the_config_file_defaults(self) -> None:
        assert registry.spec("embedded_tools.system_commands.enabled").default is False
        assert registry.spec("embedded_tools.filesystem.mode").default == "read"
        assert registry.spec("conversation.max_tool_iterations").default == 25
        assert registry.spec("voice.engine").default == "browser"
        assert registry.spec("mode").default == "personal"
