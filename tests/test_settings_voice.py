"""Voice settings registration, plus the merged bool-key regression."""

from __future__ import annotations

from spark.web.endpoints import settings as settings_mod


class TestVoiceSecret:
    def test_api_key_is_a_secret(self) -> None:
        assert "voice.elevenlabs.api_key" in settings_mod._SECRET_KEYS

    def test_secret_name_matches_what_the_engine_reads(self) -> None:
        # load_config() reads ctx.secrets.get("elevenlabs_api_key"); if this
        # derivation ever drifts, the key silently stops being found.
        assert settings_mod._secret_name("voice.elevenlabs.api_key") == "elevenlabs_api_key"


class TestIntKeys:
    def test_numeric_voice_settings_are_ints(self) -> None:
        assert "voice.elevenlabs.monthly_character_cap" in settings_mod._INT_KEYS
        assert "voice.elevenlabs.cache_max_mb" in settings_mod._INT_KEYS


class TestBoolKeysRegression:
    def test_email_bool_keys_survive(self) -> None:
        # Regression: _BOOL_STRING_KEYS was declared twice and the second
        # declaration clobbered the first, dropping these two keys.
        assert "embedded_tools.email.use_tls" in settings_mod._BOOL_STRING_KEYS
        assert "embedded_tools.email.require_approval" in settings_mod._BOOL_STRING_KEYS

    def test_system_command_bool_key_survives(self) -> None:
        assert "embedded_tools.system_commands.require_approval" in settings_mod._BOOL_STRING_KEYS

    def test_voice_bool_key_registered(self) -> None:
        assert "voice.elevenlabs.cache_enabled" in settings_mod._BOOL_STRING_KEYS
