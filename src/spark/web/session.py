"""Personal-mode browser session: one at a time, with inactivity and absolute expiry.

Each session carries its own CSRF token. Session identifiers are compared in
constant time. Server mode replaces this with warden.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Session:
    session_id: str
    csrf_token: str
    created_at: float
    last_active: float


def _matches(presented: str, expected: str) -> bool:
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class SessionManager:
    """Manages a single active browser session.

    ``timeout_minutes`` ends a session after that much inactivity and
    ``max_age_hours`` after that long regardless of activity; zero or less
    turns either off.
    """

    def __init__(
        self,
        timeout_minutes: int = 60,
        max_age_hours: float = 24,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle = timeout_minutes * 60.0 if timeout_minutes > 0 else None
        self._max_age = max_age_hours * 3600.0 if max_age_hours > 0 else None
        self._clock = clock
        self._lock = threading.Lock()
        self._session: _Session | None = None

    def create(self) -> str:
        """Create a new session, replacing any existing one."""
        now = self._clock()
        session = _Session(
            session_id=secrets.token_hex(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_active=now,
        )
        with self._lock:
            self._session = session
        return session.session_id

    def validate(self, session_id: str) -> bool:
        """Whether ``session_id`` is the live session; refreshes its activity."""
        with self._lock:
            session = self._current(session_id)
            if session is None:
                return False
            session.last_active = self._clock()
            return True

    def csrf_token(self, session_id: str) -> str | None:
        """The CSRF token of the live session ``session_id``, without refreshing it."""
        with self._lock:
            session = self._current(session_id)
            return session.csrf_token if session is not None else None

    def destroy(self, session_id: str | None = None) -> None:
        """End the session (only if it is ``session_id``, when one is given)."""
        with self._lock:
            session = self._session
            if session is None:
                return
            if session_id is None or _matches(session_id, session.session_id):
                self._session = None

    def _current(self, session_id: str) -> _Session | None:
        session = self._session
        if session is None or not _matches(session_id, session.session_id):
            return None
        now = self._clock()
        idle_expired = self._idle is not None and now - session.last_active > self._idle
        too_old = self._max_age is not None and now - session.created_at > self._max_age
        if idle_expired or too_old:
            self._session = None
            return None
        return session
