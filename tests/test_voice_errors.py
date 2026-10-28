"""Mapping ElevenLabs HTTP failures to fallback reasons."""

from __future__ import annotations

from spark.voice.errors import VoiceUnavailable, classify_error


class TestClassifyError:
    def test_auth_failures(self) -> None:
        assert classify_error(401, None) == "bad_key"
        assert classify_error(403, None) == "bad_key"

    def test_quota_by_status_code(self) -> None:
        assert classify_error(402, None) == "quota"

    def test_quota_by_body_marker_overrides_auth(self) -> None:
        # Legacy behaviour: 401/422 carrying a quota_exceeded marker is a quota
        # problem, not a bad key. The key is fine; the credits are gone.
        body = {"detail": {"status": "quota_exceeded"}}
        assert classify_error(401, body) == "quota"
        assert classify_error(422, body) == "quota"
        assert classify_error(429, body) == "quota"

    def test_quota_marker_in_code_field(self) -> None:
        assert classify_error(400, {"detail": {"code": "quota_exceeded"}}) == "quota"

    def test_rate_limit(self) -> None:
        assert classify_error(429, None) == "rate_limited"

    def test_validation(self) -> None:
        assert classify_error(400, None) == "bad_request"
        assert classify_error(422, None) == "bad_request"

    def test_server(self) -> None:
        assert classify_error(500, None) == "server"
        assert classify_error(503, None) == "server"

    def test_unknown_status_is_server(self) -> None:
        assert classify_error(418, None) == "server"

    def test_malformed_body_never_raises(self) -> None:
        assert classify_error(401, {"detail": "a string, not a dict"}) == "bad_key"
        assert classify_error(401, {"nonsense": True}) == "bad_key"


def test_voice_unavailable_carries_reason() -> None:
    err = VoiceUnavailable("quota")
    assert err.reason == "quota"
    assert "quota" in str(err)
