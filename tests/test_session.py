"""Tests for the SessionManager."""

from datetime import datetime, timedelta, timezone

from spark.web.session import SessionManager


class TestSessionManager:
    def test_create_returns_session_id(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        session_id = sm.create()
        assert isinstance(session_id, str)
        assert len(session_id) == 64  # hex(32 bytes)

    def test_validate_valid_session(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        session_id = sm.create()
        assert sm.validate(session_id) is True

    def test_validate_wrong_session(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        sm.create()
        assert sm.validate("wrong-id") is False

    def test_validate_no_session(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        assert sm.validate("anything") is False

    def test_create_replaces_previous(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        id1 = sm.create()
        id2 = sm.create()
        assert id1 != id2
        assert sm.validate(id1) is False
        assert sm.validate(id2) is True

    def test_destroy(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        session_id = sm.create()
        sm.destroy()
        assert sm.validate(session_id) is False

    def test_timeout_expires_session(self) -> None:
        sm = SessionManager(timeout_minutes=1)
        session_id = sm.create()
        # Manually backdate the session
        assert sm._session is not None
        sm._session.last_active = datetime.now(timezone.utc) - timedelta(minutes=2)
        assert sm.validate(session_id) is False
        # Session should be destroyed
        assert sm._session is None

    def test_validate_refreshes_activity(self) -> None:
        sm = SessionManager(timeout_minutes=60)
        session_id = sm.create()
        assert sm._session is not None
        old_active = sm._session.last_active
        # Validate should update last_active
        sm.validate(session_id)
        assert sm._session.last_active >= old_active

    def test_no_timeout(self) -> None:
        sm = SessionManager(timeout_minutes=0)
        session_id = sm.create()
        # Manually backdate significantly
        assert sm._session is not None
        sm._session.last_active = datetime.now(timezone.utc) - timedelta(days=365)
        assert sm.validate(session_id) is True

    def test_negative_timeout_means_no_timeout(self) -> None:
        sm = SessionManager(timeout_minutes=-1)
        session_id = sm.create()
        assert sm._session is not None
        sm._session.last_active = datetime.now(timezone.utc) - timedelta(days=365)
        assert sm.validate(session_id) is True
