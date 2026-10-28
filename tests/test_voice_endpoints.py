"""Voice endpoints: config never leaks the key; failures signal fallback."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.voice.engine import VoiceConfig
from spark.voice.errors import VoiceUnavailable
from spark.web.server import create_app


def _mock_settings_get(key: str, default=None, *, cast=None):
    values = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
        "interface.ssl.enabled": False,
    }
    val = values.get(key, default)
    if cast is not None and val is not None:
        val = cast(val)
    return val


class FakeEngine:
    def __init__(self, *, available=True, audio=b"mp3", raises=None) -> None:
        self._available = available
        self._audio = audio
        self._raises = raises
        self.config = VoiceConfig(
            enabled=available,
            api_key="secret-key-do-not-leak" if available else "",
            model_id="eleven_flash_v2_5",
            default_voice_id="v-default",
            monthly_character_cap=1000,
        )
        self.calls: list[tuple] = []

    def available(self) -> bool:
        return self._available

    def voices(self) -> list[dict]:
        if not self._available:
            return []
        return [{"voice_id": "v1", "name": "George", "labels": {}, "preview_url": ""}]

    def synthesise(self, text, voice_id=None, *, user_guid, conversation_id=None):
        self.calls.append((text, voice_id, user_guid, conversation_id))
        if self._raises:
            raise VoiceUnavailable(self._raises)
        return self._audio


@pytest.fixture()
def client(tmp_path) -> TestClient:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)

    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)

    mgr = MagicMock()
    mgr._db = conn
    app.state.conversation_manager = mgr
    app.state.user_guid = "u1"
    app.state.voice_engine = FakeEngine()

    c = TestClient(app)
    code = app.state.auth.generate_code()
    resp = c.post("/api/auth", data={"code": code}, follow_redirects=False)
    c.cookies.set("spark_session", resp.cookies.get("spark_session", ""))
    return c


class TestConfig:
    def test_never_leaks_the_key(self, client) -> None:
        r = client.get("/voice/api/config")
        assert r.status_code == 200
        assert "secret-key-do-not-leak" not in r.text
        data = r.json()
        assert "api_key" not in data
        assert data["has_key"] is True
        assert data["enabled"] is True
        assert data["interaction_mode"] == "listen_along"

    def test_disabled_shape(self, client) -> None:
        client.app.state.voice_engine = FakeEngine(available=False)
        data = client.get("/voice/api/config").json()
        assert data["enabled"] is False
        assert data["has_key"] is False


class TestVoices:
    def test_lists_voices(self, client) -> None:
        data = client.get("/voice/api/voices").json()
        assert data[0]["voice_id"] == "v1"

    def test_empty_when_disabled(self, client) -> None:
        client.app.state.voice_engine = FakeEngine(available=False)
        assert client.get("/voice/api/voices").json() == []


class TestSpeak:
    def test_returns_audio(self, client) -> None:
        r = client.post(
            "/voice/api/speak",
            json={"text": "Hello", "voice_id": "v1", "conversation_id": 3},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/mpeg"
        assert r.content == b"mp3"

        engine = client.app.state.voice_engine
        assert engine.calls == [("Hello", "v1", "u1", 3)]

    def test_empty_text_is_400(self, client) -> None:
        assert client.post("/voice/api/speak", json={"text": "  "}).status_code == 400

    def test_failure_is_503_with_reason(self, client) -> None:
        client.app.state.voice_engine = FakeEngine(raises="quota")
        r = client.post("/voice/api/speak", json={"text": "Hello"})
        assert r.status_code == 503
        assert r.json()["reason"] == "quota"

    def test_disabled_is_503_disabled(self, client) -> None:
        client.app.state.voice_engine = FakeEngine(available=False, raises="disabled")
        r = client.post("/voice/api/speak", json={"text": "Hello"})
        assert r.status_code == 503
        assert r.json()["reason"] == "disabled"


class TestUsage:
    def test_usage_shape(self, client) -> None:
        data = client.get("/voice/api/usage").json()
        assert data["characters_this_month"] == 0
        assert data["cap"] == 1000
        assert data["by_conversation"] == []
