"""Personal-mode login codes: single use, short-lived and rate-limited.

A code is shown in the terminal (and carried by the auto-login URL opened at
start-up). It can be redeemed once, within its lifetime. Each client address
may fail a limited number of times within a window before it is locked out for
a while; a locked-out address is refused even with the right code, and the
code stays live for someone else. Codes are never logged here.

Server mode replaces this with warden; nothing outside ``spark.web`` uses it.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from starlette.requests import HTTPConnection

if TYPE_CHECKING:
    from spark.web.session import SessionManager

logger = logging.getLogger(__name__)

SESSION_COOKIE = "spark_session"
LOGIN_CSRF_COOKIE = "spark_login_csrf"
LOGIN_PATH = "/api/auth"

_MAX_TRACKED_CLIENTS = 4096


@runtime_checkable
class WebAuthenticator(Protocol):
    """What the web layer asks of whatever signs users in.

    Personal mode answers from its login code and single session
    (:class:`PersonalAuthenticator`); server mode answers from warden. The auth
    middleware in ``spark.web.server`` and the CSRF check in
    ``spark.web.security`` use ``app.state.authenticator`` and nothing else.
    """

    def is_authenticated(self, conn: HTTPConnection) -> bool:
        """Whether the request belongs to a signed-in session (refreshing it)."""
        ...

    def csrf_token(self, conn: HTTPConnection) -> str | None:
        """The CSRF token this request must carry, or None if it cannot carry one."""
        ...


class PersonalAuthenticator:
    """Personal mode: one session cookie, and a login-form token before sign-in."""

    def __init__(self, sessions: SessionManager) -> None:
        self.sessions = sessions

    def is_authenticated(self, conn: HTTPConnection) -> bool:
        session_id = conn.cookies.get(SESSION_COOKIE)
        return bool(session_id) and self.sessions.validate(session_id or "")

    def csrf_token(self, conn: HTTPConnection) -> str | None:
        if conn.url.path == LOGIN_PATH:
            return conn.cookies.get(LOGIN_CSRF_COOKIE) or None
        return self.sessions.csrf_token(conn.cookies.get(SESSION_COOKIE) or "")


class RedeemOutcome(enum.Enum):
    """The result of presenting a login code."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    LOCKED_OUT = "locked_out"


@dataclass(frozen=True)
class LoginPolicy:
    """Lifetime of a code and the per-address failure limit."""

    code_ttl_seconds: float = 300.0
    max_failures: int = 5
    failure_window_seconds: float = 300.0
    lockout_seconds: float = 900.0


@dataclass
class _Code:
    digest: bytes
    expires_at: float


@dataclass
class _Failures:
    times: list[float] = field(default_factory=list)
    locked_until: float = 0.0


def _digest(code: str) -> bytes:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).digest()


class AuthManager:
    """Issues and redeems one live login code at a time."""

    def __init__(
        self,
        policy: LoginPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy or LoginPolicy()
        self._clock = clock
        self._lock = threading.Lock()
        self._code: _Code | None = None
        self._failures: dict[str, _Failures] = {}

    @property
    def policy(self) -> LoginPolicy:
        return self._policy

    def generate_code(self) -> str:
        """Issue a new 8-character code, replacing any live one."""
        with self._lock:
            return self._issue()

    def ensure_code(self) -> str | None:
        """Issue a new code only when none is live; return it, or None."""
        with self._lock:
            if self._live_code() is not None:
                return None
            return self._issue()

    def _issue(self) -> str:
        code = secrets.token_hex(4).upper()
        self._code = _Code(_digest(code), self._clock() + self._policy.code_ttl_seconds)
        return code

    def is_locked_out(self, client: str) -> bool:
        with self._lock:
            return self._locked(client, self._clock())

    def redeem(self, code: str, client: str) -> RedeemOutcome:
        """Spend ``code`` for ``client`` if it is live and the client is not locked out."""
        presented = _digest(code)
        with self._lock:
            now = self._clock()
            if self._locked(client, now):
                logger.warning("Sign-in refused for %s: locked out after repeated failures", client)
                return RedeemOutcome.LOCKED_OUT
            live = self._live_code()
            if live is not None and hmac.compare_digest(presented, live.digest):
                self._code = None
                self._failures.pop(client, None)
                return RedeemOutcome.ACCEPTED
            if live is None:
                # Compare anyway so a missing code takes as long as a wrong one.
                hmac.compare_digest(presented, presented)
            self._record_failure(client, now)
            return RedeemOutcome.REFUSED

    def _live_code(self) -> _Code | None:
        if self._code is not None and self._clock() >= self._code.expires_at:
            self._code = None
        return self._code

    def _locked(self, client: str, now: float) -> bool:
        entry = self._failures.get(client)
        return entry is not None and now < entry.locked_until

    def _record_failure(self, client: str, now: float) -> None:
        policy = self._policy
        if client not in self._failures and len(self._failures) >= _MAX_TRACKED_CLIENTS:
            self._prune(now)
        entry = self._failures.setdefault(client, _Failures())
        entry.times = [t for t in entry.times if now - t < policy.failure_window_seconds]
        entry.times.append(now)
        count = len(entry.times)
        if count >= policy.max_failures:
            entry.locked_until = now + policy.lockout_seconds
            entry.times.clear()
            logger.warning(
                "Sign-in failed for %s: invalid or expired code; address locked for %d seconds "
                "after %d failures",
                client,
                int(policy.lockout_seconds),
                count,
            )
        else:
            logger.warning(
                "Sign-in failed for %s: invalid or expired code (%d of %d before lock-out)",
                client,
                count,
                policy.max_failures,
            )

    def _prune(self, now: float) -> None:
        window = self._policy.failure_window_seconds
        stale = [
            client
            for client, entry in self._failures.items()
            if now >= entry.locked_until and all(now - t >= window for t in entry.times)
        ]
        for client in stale:
            del self._failures[client]
        while len(self._failures) >= _MAX_TRACKED_CLIENTS:
            del self._failures[next(iter(self._failures))]
