"""ElevenLabs HTTP client: request shape, retries, and failure mapping."""

from __future__ import annotations

import pytest

from spark.voice.elevenlabs import ElevenLabsClient
from spark.voice.errors import VoiceUnavailable


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", payload=None) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """Records calls and returns queued responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url, headers=None, params=None, json=None):
        self.posts.append({"url": url, "headers": headers, "params": params, "json": json})
        return self.responses.pop(0)

    def get(self, url, headers=None, params=None):
        self.gets.append({"url": url, "headers": headers, "params": params})
        return self.responses.pop(0)


class TestSynthesise:
    def test_success_returns_audio_bytes(self) -> None:
        http = FakeHttp([FakeResponse(200, content=b"ID3-audio")])
        client = ElevenLabsClient("k-123", http=http)
        assert client.synthesise("Hello there.", "voice-abc") == b"ID3-audio"

    def test_request_shape(self) -> None:
        http = FakeHttp([FakeResponse(200, content=b"a")])
        client = ElevenLabsClient("k-123", model_id="eleven_flash_v2_5", http=http)
        client.synthesise("Hello there.", "voice-abc")

        call = http.posts[0]
        assert call["url"].endswith("/v1/text-to-speech/voice-abc/stream")
        assert call["headers"]["xi-api-key"] == "k-123"
        assert call["params"]["output_format"] == "mp3_44100_128"
        assert call["json"]["text"] == "Hello there."
        assert call["json"]["model_id"] == "eleven_flash_v2_5"
        # speed is deliberately never sent: the ElevenLabs docs contradict
        # themselves on its valid range.
        assert "speed" not in call["json"]["voice_settings"]
        assert call["json"]["voice_settings"]["style"] == 0

    def test_bad_key_raises(self) -> None:
        http = FakeHttp([FakeResponse(401, payload={"detail": {"status": "invalid"}})])
        client = ElevenLabsClient("bad", http=http)
        with pytest.raises(VoiceUnavailable) as exc:
            client.synthesise("hi", "v")
        assert exc.value.reason == "bad_key"

    def test_quota_raises_without_retry(self) -> None:
        http = FakeHttp([FakeResponse(402, payload={})])
        client = ElevenLabsClient("k", http=http)
        with pytest.raises(VoiceUnavailable) as exc:
            client.synthesise("hi", "v")
        assert exc.value.reason == "quota"
        assert len(http.posts) == 1  # no point retrying an exhausted quota

    def test_rate_limit_retries_once_then_succeeds(self) -> None:
        http = FakeHttp([FakeResponse(429, payload={}), FakeResponse(200, content=b"ok")])
        client = ElevenLabsClient("k", http=http, retry_delay=0)
        assert client.synthesise("hi", "v") == b"ok"
        assert len(http.posts) == 2

    def test_server_error_retries_once_then_gives_up(self) -> None:
        http = FakeHttp([FakeResponse(503, payload={}), FakeResponse(503, payload={})])
        client = ElevenLabsClient("k", http=http, retry_delay=0)
        with pytest.raises(VoiceUnavailable) as exc:
            client.synthesise("hi", "v")
        assert exc.value.reason == "server"
        assert len(http.posts) == 2

    def test_network_exception_maps_to_network(self) -> None:
        class Boom:
            def post(self, *a, **k):
                raise OSError("connection reset")

        client = ElevenLabsClient("k", http=Boom())
        with pytest.raises(VoiceUnavailable) as exc:
            client.synthesise("hi", "v")
        assert exc.value.reason == "network"


class TestListVoices:
    def test_uses_v2_premade_and_maps_fields(self) -> None:
        payload = {
            "voices": [
                {
                    "voice_id": "v1",
                    "name": "George",
                    "labels": {"accent": "British"},
                    "preview_url": "https://x/y.mp3",
                    "category": "premade",
                    "extra": "ignored",
                }
            ]
        }
        http = FakeHttp([FakeResponse(200, payload=payload)])
        client = ElevenLabsClient("k", http=http)
        voices = client.list_voices()

        # /v1/voices is deprecated; we must call /v2/voices.
        assert http.gets[0]["url"].endswith("/v2/voices")
        assert http.gets[0]["params"]["category"] == "premade"
        assert voices == [
            {
                "voice_id": "v1",
                "name": "George",
                "labels": {"accent": "British"},
                "preview_url": "https://x/y.mp3",
            }
        ]

    def test_result_is_cached(self) -> None:
        payload = {"voices": [{"voice_id": "v1", "name": "A"}]}
        http = FakeHttp([FakeResponse(200, payload=payload)])
        client = ElevenLabsClient("k", http=http)
        client.list_voices()
        client.list_voices()
        assert len(http.gets) == 1  # second call served from memory

    def test_failure_returns_empty_not_raises(self) -> None:
        http = FakeHttp([FakeResponse(500, payload={})])
        client = ElevenLabsClient("k", http=http)
        assert client.list_voices() == []
