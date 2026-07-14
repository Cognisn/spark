"""Voice engine: cache/cap/API ordering, billing, and failure propagation."""

from __future__ import annotations

import pytest

from spark.database import Database, voice_usage
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.voice.engine import VoiceConfig, VoiceEngine
from spark.voice.errors import VoiceUnavailable


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)
    return conn


class FakeClient:
    def __init__(self, audio: bytes = b"audio", raises: str | None = None) -> None:
        self.audio = audio
        self.raises = raises
        self.calls = 0

    def synthesise(self, text: str, voice_id: str) -> bytes:
        self.calls += 1
        if self.raises:
            raise VoiceUnavailable(self.raises)
        return self.audio

    def list_voices(self) -> list[dict]:
        return [{"voice_id": "v1", "name": "George", "labels": {}, "preview_url": ""}]


def _config(**over) -> VoiceConfig:
    base = {
        "enabled": True,
        "api_key": "k",
        "model_id": "eleven_flash_v2_5",
        "default_voice_id": "v-default",
        "monthly_character_cap": 0,
        "cache_enabled": True,
        "cache_max_mb": 200,
        "interaction_mode": "listen_along",
    }
    base.update(over)
    return VoiceConfig(**base)


class TestAvailability:
    def test_unavailable_when_disabled(self, db, tmp_path) -> None:
        e = VoiceEngine(_config(enabled=False), db, tmp_path, client=FakeClient())
        assert e.available() is False

    def test_unavailable_without_key(self, db, tmp_path) -> None:
        e = VoiceEngine(_config(api_key=""), db, tmp_path, client=FakeClient())
        assert e.available() is False

    def test_available_when_enabled_with_key(self, db, tmp_path) -> None:
        e = VoiceEngine(_config(), db, tmp_path, client=FakeClient())
        assert e.available() is True

    def test_synthesise_when_disabled_raises_disabled(self, db, tmp_path) -> None:
        e = VoiceEngine(_config(enabled=False), db, tmp_path, client=FakeClient())
        with pytest.raises(VoiceUnavailable) as exc:
            e.synthesise("hi", "v1", user_guid="u1")
        assert exc.value.reason == "disabled"


class TestSynthesise:
    def test_calls_api_and_bills_characters(self, db, tmp_path) -> None:
        client = FakeClient(b"mp3")
        e = VoiceEngine(_config(), db, tmp_path, client=client)
        assert e.synthesise("Hello", "v1", user_guid="u1", conversation_id=3) == b"mp3"
        assert client.calls == 1
        assert voice_usage.characters_this_month(db, "u1") == len("Hello")

    def test_falls_back_to_default_voice(self, db, tmp_path) -> None:
        client = FakeClient()
        e = VoiceEngine(_config(), db, tmp_path, client=client)
        e.synthesise("Hello", None, user_guid="u1")
        assert client.calls == 1  # no voice given, the default was used

    def test_cache_hit_skips_api_and_bills_nothing(self, db, tmp_path) -> None:
        client = FakeClient(b"mp3")
        e = VoiceEngine(_config(), db, tmp_path, client=client)
        e.synthesise("Hello", "v1", user_guid="u1")
        e.synthesise("Hello", "v1", user_guid="u1")  # identical -> cached

        assert client.calls == 1
        assert voice_usage.characters_this_month(db, "u1") == len("Hello")

    def test_cache_disabled_always_calls_api(self, db, tmp_path) -> None:
        client = FakeClient(b"mp3")
        e = VoiceEngine(_config(cache_enabled=False), db, tmp_path, client=client)
        e.synthesise("Hello", "v1", user_guid="u1")
        e.synthesise("Hello", "v1", user_guid="u1")
        assert client.calls == 2

    def test_api_failure_propagates_reason(self, db, tmp_path) -> None:
        e = VoiceEngine(_config(), db, tmp_path, client=FakeClient(raises="quota"))
        with pytest.raises(VoiceUnavailable) as exc:
            e.synthesise("Hello", "v1", user_guid="u1")
        assert exc.value.reason == "quota"
        assert voice_usage.characters_this_month(db, "u1") == 0  # nothing billed


class TestCap:
    def test_cap_blocks_new_synthesis(self, db, tmp_path) -> None:
        voice_usage.record(db, "u1", 100)
        client = FakeClient()
        e = VoiceEngine(_config(monthly_character_cap=100), db, tmp_path, client=client)
        with pytest.raises(VoiceUnavailable) as exc:
            e.synthesise("Hello", "v1", user_guid="u1")
        assert exc.value.reason == "cap"
        assert client.calls == 0

    def test_cached_audio_still_plays_over_cap(self, db, tmp_path) -> None:
        # Cache is checked before the cap: replaying costs nothing, so there is
        # no reason to deny it.
        client = FakeClient(b"mp3")
        e = VoiceEngine(_config(monthly_character_cap=1000), db, tmp_path, client=client)
        e.synthesise("Hello", "v1", user_guid="u1")

        voice_usage.record(db, "u1", 1000)  # now over the cap
        assert e.synthesise("Hello", "v1", user_guid="u1") == b"mp3"
        assert client.calls == 1
