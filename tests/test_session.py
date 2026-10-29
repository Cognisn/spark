"""Tests for the SessionManager."""

from __future__ import annotations

import pytest

import spark.web.session as session_module
from spark.web.session import SessionManager


class FakeClock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def _manager(
    clock: FakeClock, timeout_minutes: int = 60, max_age_hours: float = 24
) -> SessionManager:
    return SessionManager(timeout_minutes=timeout_minutes, max_age_hours=max_age_hours, clock=clock)


class TestSessionManager:
    def test_create_returns_session_id(self, clock: FakeClock) -> None:
        session_id = _manager(clock).create()
        assert isinstance(session_id, str)
        assert len(session_id) == 64  # hex(32 bytes)

    def test_validate_valid_session(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        assert sm.validate(sm.create()) is True

    def test_validate_wrong_session(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        sm.create()
        assert sm.validate("wrong-id") is False

    def test_validate_empty_session(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        sm.create()
        assert sm.validate("") is False

    def test_validate_non_ascii_is_refused_not_raised(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        sm.create()
        assert sm.validate("é" * 64) is False

    def test_validate_no_session(self, clock: FakeClock) -> None:
        assert _manager(clock).validate("anything") is False

    def test_create_replaces_previous(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        id1 = sm.create()
        id2 = sm.create()
        assert id1 != id2
        assert sm.validate(id1) is False
        assert sm.validate(id2) is True


class TestExpiry:
    def test_inactivity_expires_session(self, clock: FakeClock) -> None:
        sm = _manager(clock, timeout_minutes=1)
        session_id = sm.create()
        clock.advance(61)
        assert sm.validate(session_id) is False
        clock.advance(-61)
        assert sm.validate(session_id) is False  # destroyed, not just stale

    def test_activity_keeps_session_alive(self, clock: FakeClock) -> None:
        sm = _manager(clock, timeout_minutes=1)
        session_id = sm.create()
        for _ in range(5):
            clock.advance(50)
            assert sm.validate(session_id) is True

    def test_absolute_lifetime_ends_an_active_session(self, clock: FakeClock) -> None:
        sm = _manager(clock, timeout_minutes=60, max_age_hours=2)
        session_id = sm.create()
        for _ in range(7):
            clock.advance(1000)
            assert sm.validate(session_id) is True
        clock.advance(300)  # 7300 seconds since creation
        assert sm.validate(session_id) is False

    def test_no_inactivity_timeout(self, clock: FakeClock) -> None:
        sm = _manager(clock, timeout_minutes=0, max_age_hours=0)
        session_id = sm.create()
        clock.advance(365 * 86400)
        assert sm.validate(session_id) is True

    def test_negative_timeout_means_no_timeout(self, clock: FakeClock) -> None:
        sm = _manager(clock, timeout_minutes=-1, max_age_hours=-1)
        session_id = sm.create()
        clock.advance(365 * 86400)
        assert sm.validate(session_id) is True


class TestSignOut:
    def test_destroy_invalidates(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        session_id = sm.create()
        sm.destroy(session_id)
        assert sm.validate(session_id) is False
        assert sm.csrf_token(session_id) is None

    def test_destroy_with_a_stale_id_leaves_the_current_session(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        old = sm.create()
        current = sm.create()
        sm.destroy(old)
        assert sm.validate(current) is True


class TestCsrfToken:
    def test_valid_session_has_a_token(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        session_id = sm.create()
        token = sm.csrf_token(session_id)
        assert token is not None and len(token) >= 32
        assert token != session_id

    def test_token_is_stable_within_a_session(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        session_id = sm.create()
        assert sm.csrf_token(session_id) == sm.csrf_token(session_id)

    def test_new_session_gets_a_new_token(self, clock: FakeClock) -> None:
        sm = _manager(clock)
        first = sm.csrf_token(sm.create())
        second = sm.csrf_token(sm.create())
        assert first != second

    def test_wrong_or_expired_session_has_no_token(self, clock: FakeClock) -> None:
        sm = _manager(clock, timeout_minutes=1)
        session_id = sm.create()
        assert sm.csrf_token("wrong-id") is None
        clock.advance(61)
        assert sm.csrf_token(session_id) is None


class TestConstantTime:
    def test_validate_compares_with_compare_digest(
        self, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, object]] = []
        real = session_module.hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(session_module.hmac, "compare_digest", spy)
        sm = _manager(clock)
        session_id = sm.create()
        assert sm.validate("0" * 64) is False
        assert sm.validate(session_id) is True
        assert len(calls) == 2
