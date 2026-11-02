"""Server-mode sign-in for the web layer: warden sessions in secure cookies (A11).

:class:`WardenAuthenticator` is ``app.state.authenticator`` in server mode. It
implements :class:`spark.web.auth.WebAuthenticator`, so the request guard
(Origin and CSRF, :mod:`spark.web.security`), ``csrf.js`` and the templates'
``csrf_token`` helper work unchanged, and adds :meth:`~WardenAuthenticator.admit`,
which the auth middleware awaits on every request.

Cookies (all HttpOnly, Secure, SameSite=Strict, never a Domain):

* :data:`ACCESS_COOKIE` (``__Host-``, path ``/``): warden's short-lived access
  token. It is kept as long as the refresh token, so an expired access token
  still reaches the server and can be told apart from no session at all.
* :data:`REFRESH_COOKIE` (``__Secure-``, path :data:`REFRESH_PATH` only): the
  rotating refresh token. The browser sends it nowhere else, and nothing here
  reads it anywhere else.

On each request the access token is authenticated through warden. When it has
expired (or a page load finds it about to), the request is sent through the
refresh path: a navigation is redirected (303) to ``REFRESH_PATH?next=...``,
which rotates both cookies and redirects back; a fetch or event stream gets 401
with ``X-Spark-Session: refresh``, and ``csrf.js`` posts to the refresh path
once (serialised) and retries. A request with no usable session is sent to the
sign-in page (navigation) or answered 401 with ``X-Spark-Session: sign-in``.

CSRF: a session's token is an HMAC-SHA256, under a per-instance key kept in
konfig's secrets store, of its warden session id, so it survives refreshes and
a token from any other session never matches. The sign-in form has its own
token in a cookie scoped to the sign-in path; a second-factor challenge's
token is bound to the challenge.

API keys: ``Authorization: Bearer wdn_...`` authenticates through warden's API
keys (the key's scopes intersected with its user's permissions). Such a request
carries no ambient credential, so it needs no CSRF token; browser provenance,
if present, must still be the public origin. A bearer is never combined with a
session cookie: a request carrying both is refused, so a cookie can never ride
on a bearer's CSRF exemption. Only API keys are accepted as bearers.

Every refusal fails closed: no identity, no permissions, and endpoints never
fall back to the process's own user guid (:func:`spark.identity.access.current_user_guid`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response
from warden.identity import AuthenticationRefused, Principal, TokenPair
from warden.identity.http import SourceIp, client_ip, refusal

from spark.database import user_identities
from spark.identity.access import STATE_KEY, SparkIdentity, identity_from_principal

if TYPE_CHECKING:
    from spark.identity.runtime import IdentityRuntime

__all__ = [
    "ACCESS_COOKIE",
    "CHALLENGE_COOKIE",
    "CHALLENGE_PATH",
    "LOGOUT_PATH",
    "PUBLIC_PATHS",
    "PUBLIC_PREFIXES",
    "REFRESH_COOKIE",
    "REFRESH_PATH",
    "SESSION_HEADER",
    "SESSION_PATH",
    "SIGN_IN_CSRF_COOKIE",
    "SIGN_IN_PATH",
    "SIGN_OUT_PATH",
    "SignedClaims",
    "WardenAuthenticator",
    "clear_session_cookies",
    "csrf_token_for",
    "is_navigation",
    "safe_next",
    "set_session_cookies",
    "signed_claims",
    "source_ip",
]

logger = logging.getLogger(__name__)

ACCESS_COOKIE = "__Host-spark-access"
REFRESH_COOKIE = "__Secure-spark-refresh"
SIGN_IN_CSRF_COOKIE = "__Secure-spark-sign-in"
CHALLENGE_COOKIE = "__Secure-spark-challenge"

SIGN_IN_PATH = "/auth/sign-in"
SIGN_OUT_PATH = "/auth/sign-out"
REFRESH_PATH = "/auth/session/refresh"
SESSION_PATH = "/auth/session"
CHALLENGE_PATH = "/auth/challenge"
# The menu's sign-out form (shared with personal mode).
LOGOUT_PATH = "/logout"

# Answers a fetch or stream that needs a session: "refresh" or "sign-in".
SESSION_HEADER = "X-Spark-Session"

# Reachable without a live session. Sign-out and refresh check what they need
# themselves (their CSRF token is bound to the session the cookie names).
# The start-up status is the container health check's (it reveals only the
# start-up stage). Step 5 adds its screens (invitation, password reset, SSO
# completion) here.
STATUS_PATH = "/loading/api/status"
PUBLIC_PATHS = frozenset(
    {
        SIGN_IN_PATH,
        SIGN_OUT_PATH,
        LOGOUT_PATH,
        REFRESH_PATH,
        CHALLENGE_PATH,
        STATUS_PATH,
        "/favicon.ico",
    }
)
PUBLIC_PREFIXES = ("/static/", CHALLENGE_PATH + "/")

# A page load whose access token expires within this many seconds is refreshed first.
REFRESH_MARGIN_SECONDS = 30

_STATE_SESSION = "warden_session_id"
_STATE_BEARER = "warden_api_key"
_STATE_PRINCIPAL = "warden_principal"
_NO_STORE = {"Cache-Control": "no-store"}
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_MAX_TOKEN = 4096
_HEADER_KEYS = frozenset({"alg", "typ", "kid"})


def csrf_token_for(key: bytes, kind: str, value: str) -> str:
    """The CSRF token bound to ``value`` (a warden session id, or a challenge)."""
    mac = hmac.new(key, f"spark-csrf-v1\x00{kind}\x00{value}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode("ascii")


def safe_next(value: object) -> str:
    """A same-origin path to return to, or ``/``.

    Only a path starting with a single ``/`` is kept: never a scheme, a
    protocol-relative ``//host`` or a backslash some browsers read as ``/``.
    """
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 2048:
        return "/"
    if value.startswith(("//", "/\\")) or "\\" in value or any(ord(c) < 0x20 for c in value):
        return "/"
    return value


def _request_target(request: HTTPConnection) -> str:
    query = request.url.query
    return safe_next(request.url.path + (f"?{query}" if query else ""))


def is_navigation(request: HTTPConnection) -> bool:
    """Whether a request is a browser page load (as opposed to a fetch or a stream)."""
    if request.scope.get("method") not in ("GET", "HEAD"):
        return False
    mode = request.headers.get("sec-fetch-mode")
    if mode is not None:
        return mode == "navigate"
    return "text/html" in request.headers.get("accept", "")


def source_ip(runtime: IdentityRuntime, request: HTTPConnection) -> SourceIp:
    """The client address warden records, believing only the configured proxies."""
    return client_ip(request, runtime.config.audit.trusted_proxies)  # type: ignore[arg-type]


def set_session_cookies(response: Response, pair: TokenPair) -> None:
    """Hand a warden session to the browser: access and refresh cookies, never cached."""
    lifetime = pair.refresh_expires_in
    response.set_cookie(
        ACCESS_COOKIE,
        pair.access_token,
        max_age=lifetime,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=lifetime,
        path=REFRESH_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookies(response: Response) -> None:
    """Remove both session cookies (each at its own path)."""
    for name, path in ((ACCESS_COOKIE, "/"), (REFRESH_COOKIE, REFRESH_PATH)):
        response.delete_cookie(name, path=path, secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"


@dataclass(frozen=True, slots=True)
class SignedClaims:
    """An access token's claims, its signature checked but not its expiry."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    amr: tuple[str, ...]
    expires_at: int


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes] | None:
    parts = token.split(".")
    if len(token) > _MAX_TOKEN or len(parts) != 3:
        return None
    if any(_B64URL.fullmatch(p) is None for p in parts):
        return None
    try:
        header = json.loads(_unb64(parts[0]))
        claims = json.loads(_unb64(parts[1]))
        signature = _unb64(parts[2])
    except (ValueError, binascii.Error):
        return None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None
    return header, claims, signature, f"{parts[0]}.{parts[1]}".encode("ascii")


async def signed_claims(runtime: IdentityRuntime, token: str) -> SignedClaims | None:
    """The claims of an access token warden signed with a live key, expired or not.

    Used only to tell an expired session from no session, and to bind CSRF
    tokens on the refresh and sign-out paths; never to authenticate a request.
    """
    decoded = _decode(token)
    if decoded is None:
        return None
    header, claims, signature, signing_input = decoded
    kid = header.get("kid")
    if (
        header.keys() != _HEADER_KEYS
        or header.get("alg") != "EdDSA"
        or header.get("typ") != "at+jwt"
        or not isinstance(kid, str)
        or len(signature) != 64
        or claims.get("iss") != runtime.config.http.public_base_url
    ):
        return None
    if not await runtime.keys.verify(signing_input, signature, kid):
        return None
    exp, amr = claims.get("exp"), claims.get("amr")
    if not isinstance(exp, int) or isinstance(exp, bool) or not isinstance(amr, list):
        return None
    try:
        user_id = uuid.UUID(str(claims.get("sub")))
        session_id = uuid.UUID(str(claims.get("sid")))
    except ValueError:
        return None
    return SignedClaims(user_id, session_id, tuple(str(m) for m in amr), exp)


def _expires_within(token: str, seconds: int) -> bool:
    """Whether an (already authenticated) access token expires within ``seconds``."""
    decoded = _decode(token)
    exp = decoded[1].get("exp") if decoded is not None else None
    return isinstance(exp, int) and exp - time.time() <= seconds


def _bearer(request: HTTPConnection) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def session_needed(request: HTTPConnection, what: str, *, clear: bool = False) -> Response:
    """Send a request without a usable session on: to refresh, or to sign in.

    A page load is redirected (303) with a same-origin ``next``; anything else
    gets 401 with ``X-Spark-Session`` saying which.
    """
    response: Response
    if is_navigation(request):
        target = REFRESH_PATH if what == "refresh" else SIGN_IN_PATH
        query = urlencode({"next": _request_target(request)})
        response = RedirectResponse(f"{target}?{query}", status_code=303, headers=_NO_STORE)
    else:
        detail = "session expired" if what == "refresh" else "not signed in"
        response = JSONResponse(
            {"detail": detail}, status_code=401, headers={SESSION_HEADER: what, **_NO_STORE}
        )
    if clear:
        clear_session_cookies(response)
    return response


class WardenAuthenticator:
    """The web layer's view of warden (server mode). See the module docstring."""

    def __init__(self) -> None:
        self._runtime: IdentityRuntime | None = None

    @property
    def runtime(self) -> IdentityRuntime | None:
        """warden's runtime, once the lifespan has started it."""
        return self._runtime

    def bind(self, runtime: IdentityRuntime | None) -> None:
        self._runtime = runtime

    @staticmethod
    def is_public(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    # -- WebAuthenticator --------------------------------------------------------------

    def is_authenticated(self, conn: HTTPConnection) -> bool:
        return isinstance(getattr(conn.state, STATE_KEY, None), SparkIdentity)

    def csrf_token(self, conn: HTTPConnection) -> str | None:
        path = conn.url.path
        if path == SIGN_IN_PATH:
            return conn.cookies.get(SIGN_IN_CSRF_COOKIE) or None
        runtime = self._runtime
        if runtime is None:
            return None
        if path == CHALLENGE_PATH or path.startswith(CHALLENGE_PATH + "/"):
            challenge = conn.cookies.get(CHALLENGE_COOKIE)
            return csrf_token_for(runtime.csrf_key, "challenge", challenge) if challenge else None
        session = getattr(conn.state, _STATE_SESSION, None)
        if not isinstance(session, str) or not session:
            return None
        return csrf_token_for(runtime.csrf_key, "session", session)

    def csrf_exempt(self, conn: HTTPConnection) -> bool:
        """Whether the request was authenticated by an API key (no ambient credential)."""
        return getattr(conn.state, _STATE_BEARER, False) is True

    # -- The auth middleware's question ----------------------------------------------

    async def admit(self, request: Request) -> Response | None:
        """Authenticate a request; None to let it through, or the response that answers it."""
        runtime = self._runtime
        if runtime is None:
            return JSONResponse(
                {"detail": "Spark is starting; try again shortly."},
                status_code=503,
                headers=_NO_STORE,
            )
        bearer = _bearer(request)
        if bearer is not None:
            if ACCESS_COOKIE in request.cookies:
                logger.warning("Refused a request carrying both a bearer and a session cookie")
                return refusal()
            return await self._admit_api_key(runtime, request, bearer)
        token = request.cookies.get(ACCESS_COOKIE)
        if self.is_public(request.url.path):
            if token:
                claims = await signed_claims(runtime, token)
                if claims is not None:
                    setattr(request.state, _STATE_SESSION, str(claims.session_id))
            return None
        if not token:
            return session_needed(request, "sign-in")
        try:
            principal = await runtime.identity.authenticate(token)
        except AuthenticationRefused:
            claims = await signed_claims(runtime, token)
            if claims is not None and claims.expires_at <= time.time():
                return session_needed(request, "refresh")
            return session_needed(request, "sign-in", clear=True)
        if is_navigation(request) and _expires_within(token, REFRESH_MARGIN_SECONDS):
            return session_needed(request, "refresh")
        await _set_identity(request, principal)
        setattr(request.state, _STATE_SESSION, str(principal.session_id))
        return None

    async def _admit_api_key(
        self, runtime: IdentityRuntime, request: Request, token: str
    ) -> Response | None:
        keys = runtime.identity.api_keys
        if not token or not keys.looks_like_key(token):
            return refusal()
        try:
            principal = await keys.authenticate(token, source_ip=source_ip(runtime, request))
        except AuthenticationRefused:
            return refusal()
        await _set_identity(request, principal)
        setattr(request.state, _STATE_BEARER, True)
        return None


async def _set_identity(request: Request, principal: Principal) -> None:
    """Put the signed-in user on the request: their SPARK identity and guid."""
    guid = await user_identities.user_guid_for(request.app.state.database, str(principal.user_id))
    setattr(request.state, STATE_KEY, identity_from_principal(principal, guid))
    setattr(request.state, _STATE_PRINCIPAL, principal)
    request.state.user_guid = guid


def principal_of(request: HTTPConnection) -> Principal | None:
    """warden's principal behind a signed-in request (for warden calls such as sign-out)."""
    principal = getattr(request.state, _STATE_PRINCIPAL, None)
    return principal if isinstance(principal, Principal) else None


def via_api_key(request: HTTPConnection) -> bool:
    return getattr(request.state, _STATE_BEARER, False) is True
