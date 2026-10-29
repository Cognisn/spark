"""Origin and CSRF checks for the web interface.

``RequestGuardMiddleware`` refuses, with a 403 and a clear message:

* a state-changing request (POST, PUT, PATCH, DELETE) that does not come from
  one of the instance's own origins, or that does not carry the expected CSRF
  token in the ``X-CSRF-Token`` header or a ``csrf_token`` form field;
* an event stream (a path under one of ``stream_prefixes``, or a request that
  accepts ``text/event-stream``) that does not come from one of the instance's
  own origins.

Provenance is the ``Origin`` header, or failing that the ``Referer``, or
failing both a ``Sec-Fetch-Site: same-origin`` header; a request with none of
them is refused. With an explicit list of origins the ``Host`` header must be
one of them too, which keeps out pages served under another name that resolves
to this instance.

The middleware asks two callables, per request, for the policy and for the
token it should expect, so the authentication behind it can be swapped
(personal-mode sessions now, warden in server mode) without touching it.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ORIGIN_REFUSED = (
    "Request refused: it did not come from this Spark instance's own pages "
    "(Origin check failed). Open Spark at its own address and try again."
)
CSRF_REFUSED = (
    "Request refused: the CSRF token is missing or does not match this session. "
    "Reload the page and try again."
)

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_WILDCARD = ("", "0.0.0.0", "::")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_FORM_BYTES = 1024 * 1024


def _origin(scheme: str, host: str, port: int) -> str:
    name = f"[{host}]" if ":" in host else host
    suffix = "" if _DEFAULT_PORTS.get(scheme) == port else f":{port}"
    return f"{scheme}://{name}{suffix}".lower()


def _normalise(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def origin_of_url(url: str) -> str:
    """The origin (``scheme://host[:port]``) of an absolute URL, or ``""``."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}".lower()


@dataclass(frozen=True)
class OriginPolicy:
    """Which origins count as the instance's own.

    ``allowed`` origins are always accepted, and with an explicit list the
    ``Host`` header must be one of them. With ``match_host`` (an instance bound
    to every address, or one whose address is not known yet) the request's own
    ``Host`` header, with its scheme, also defines an accepted origin.
    """

    allowed: frozenset[str] = frozenset()
    match_host: bool = False

    @classmethod
    def same_host(cls, extra: Iterable[str] = ()) -> OriginPolicy:
        """Accept the origin named by each request's own Host header."""
        return cls(frozenset(_normalise(o) for o in extra), match_host=True)

    @classmethod
    def for_instance(
        cls, scheme: str, host: str, port: int, extra: Iterable[str] = ()
    ) -> OriginPolicy:
        """The origins of an instance served at ``scheme://host:port``.

        A loopback host admits all three loopback names; a wildcard bind
        address falls back to :meth:`same_host`.
        """
        bind = host.strip().lower()
        if bind in _WILDCARD:
            return cls.same_host(extra)
        names = _LOOPBACK if bind in _LOOPBACK else (bind,)
        origins = {_origin(scheme.lower(), name, port) for name in names}
        return cls(frozenset(origins | {_normalise(o) for o in extra}))

    def allows_host(self, host: str) -> bool:
        """Whether a Host header names this instance.

        The scheme is not compared: a proxy that terminates TLS forwards the
        public host name over plain HTTP.
        """
        if self.match_host:
            return True
        return host.lower() in {urlsplit(origin).netloc for origin in self.allowed}

    def allows_origin(self, origin: str, host: str, scheme: str) -> bool:
        candidate = _normalise(origin)
        if not candidate or candidate == "null":
            return False
        if not self.allows_host(host):
            return False
        if candidate in self.allowed:
            return True
        return self.match_host and bool(host) and candidate == f"{scheme}://{host}".lower()


PolicySource = Callable[[HTTPConnection], OriginPolicy]
TokenSource = Callable[[HTTPConnection], "str | None"]


def provenance_ok(policy: OriginPolicy, headers: Headers, scheme: str) -> bool:
    """Whether a request's Origin, Referer or fetch metadata names our own origin."""
    host = headers.get("host", "")
    if not policy.allows_host(host):
        return False
    origin = headers.get("origin")
    if origin is not None:
        return policy.allows_origin(origin, host, scheme)
    referer = headers.get("referer")
    if referer:
        return policy.allows_origin(origin_of_url(referer), host, scheme)
    return headers.get("sec-fetch-site", "").lower() == "same-origin"


def tokens_match(presented: str | None, expected: str | None) -> bool:
    """Constant-time comparison; a missing value on either side never matches."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class RequestGuardMiddleware:
    """ASGI middleware applying the Origin and CSRF checks described above."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: PolicySource,
        csrf_token: TokenSource,
        stream_prefixes: Sequence[str] = ("/stream/",),
    ) -> None:
        self.app = app
        self._policy = policy
        self._csrf_token = csrf_token
        self._stream_prefixes = tuple(stream_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope["method"]).upper()
        path = str(scope.get("path", ""))
        headers = Headers(scope=scope)
        unsafe = method in UNSAFE_METHODS
        stream = path.startswith(self._stream_prefixes) or "text/event-stream" in headers.get(
            "accept", ""
        )
        if not unsafe and not stream:
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        if not provenance_ok(self._policy(conn), headers, str(scope.get("scheme", "http"))):
            self._log_refusal("Origin", method, path, conn)
            await _refuse(scope, receive, send, ORIGIN_REFUSED, headers)
            return
        if not unsafe:
            await self.app(scope, receive, send)
            return

        presented = headers.get(CSRF_HEADER)
        downstream_receive = receive
        content_type = headers.get("content-type", "").lower()
        if presented is None and content_type.startswith("application/x-www-form-urlencoded"):
            body = await _read_body(receive)
            if body is not None:
                fields = parse_qs(body.decode("latin-1"), keep_blank_values=True)
                values = fields.get(CSRF_FORM_FIELD)
                presented = values[0] if values else None
                downstream_receive = _replay(body, receive)

        if not tokens_match(presented, self._csrf_token(conn)):
            self._log_refusal("CSRF", method, path, conn)
            await _refuse(scope, receive, send, CSRF_REFUSED, headers)
            return
        await self.app(scope, downstream_receive, send)

    @staticmethod
    def _log_refusal(check: str, method: str, path: str, conn: HTTPConnection) -> None:
        client = conn.client.host if conn.client else "unknown"
        logger.warning("%s check refused %s %s from %s", check, method, path, client)


async def _read_body(receive: Receive) -> bytes | None:
    """The whole request body, or None if it is larger than a form should be."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = bytes(message.get("body", b""))
        size += len(chunk)
        if size > _MAX_FORM_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _refuse(
    scope: Scope, receive: Receive, send: Send, message: str, headers: Headers
) -> None:
    if "text/html" in headers.get("accept", ""):
        body = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            "<title>Request refused</title></head><body>"
            f"<h1>Request refused</h1><p>{html.escape(message)}</p></body></html>"
        ).encode("utf-8")
        content_type = b"text/html; charset=utf-8"
    else:
        body = json.dumps({"detail": message}).encode("utf-8")
        content_type = b"application/json"
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
