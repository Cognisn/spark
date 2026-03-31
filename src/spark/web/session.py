"""Browser session management."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class _Session:
    session_id: str
    created_at: datetime
    last_active: datetime


class SessionManager:
    """Manages a single active browser session with inactivity timeout."""

    def __init__(self, timeout_minutes: int = 60) -> None:
        self._timeout = timedelta(minutes=timeout_minutes) if timeout_minutes > 0 else None
        self._session: _Session | None = None

    def create(self) -> str:
        """Create a new session, replacing any existing one."""
        session_id = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        self._session = _Session(session_id=session_id, created_at=now, last_active=now)
        return session_id

    def validate(self, session_id: str) -> bool:
        """Check if session_id is valid and not expired. Refreshes last_active on success."""
        if self._session is None or self._session.session_id != session_id:
            return False

        now = datetime.now(timezone.utc)
        if self._timeout and (now - self._session.last_active) > self._timeout:
            self._session = None
            return False

        self._session.last_active = now
        return True

    def destroy(self) -> None:
        """End the current session."""
        self._session = None
