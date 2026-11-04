"""An HTTP client for the live stack that behaves like a browser on SPARK's public origin.

The stack runs on a loopback ``http`` public base URL (D70). SPARK's session
cookies are ``Secure``, which httpx never sends over ``http``, so this client
keeps its own cookie jar (name, value and path) and sends every cookie whose
path matches, as a browser treats ``127.0.0.1`` as a secure context. Every
request carries the replica's ``Origin``; once signed in, the session's CSRF
token goes in ``X-CSRF-Token``.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from warden.identity import totp

CSRF_HEADER = "X-CSRF-Token"
NAVIGATE = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}
_TOKEN = re.compile(r'name="csrf_token" value="([^"]*)"')
_TOTP = re.compile(r'<code id="totp-secret"[^>]*>([A-Z2-7]+)</code>')


def form_token(page: httpx.Response) -> str:
    """The csrf_token of the first form on a page."""
    match = _TOKEN.search(page.text)
    assert match, page.text[:2000]
    return html.unescape(match.group(1))


# warden accepts each TOTP step once per key, so a second sign-in waits for the next step.
_last_step: dict[bytes, int] = {}


def totp_code(secret: bytes) -> str:
    step = totp.step_for(time.time())
    while _last_step.get(secret, -1) >= step:
        time.sleep(1)
        step = totp.step_for(time.time())
    _last_step[secret] = step
    return totp.code_at(secret, step)


@dataclass
class Event:
    name: str
    data: Any


@dataclass
class Browser:
    """One user's browser against one replica; ``on`` gives the same session on another."""

    base_url: str
    jar: dict[str, tuple[str, str]] = field(default_factory=dict)
    csrf: str | None = None
    timeout: float = 120.0

    def __post_init__(self) -> None:
        self.origin = self.base_url.rstrip("/")
        # No keep-alive: uvicorn closes idle connections after 5 s, and reusing one
        # it has just closed fails the request ("Server disconnected").
        self.http = httpx.Client(
            base_url=self.origin,
            timeout=self.timeout,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    def on(self, base_url: str) -> Browser:
        """The same cookies and CSRF token, sent to another replica's origin."""
        other = Browser(base_url, self.jar, self.csrf, self.timeout)
        return other

    def close(self) -> None:
        self.http.close()

    # -- cookies --------------------------------------------------------------------------

    def _cookie_header(self, path: str) -> str:
        return "; ".join(
            f"{name}={value}"
            for name, (value, scope) in self.jar.items()
            if path == scope or path.startswith(scope.rstrip("/") + "/") or scope == "/"
        )

    def _keep(self, resp: httpx.Response) -> None:
        for key, header in resp.headers.multi_items():
            if key.lower() != "set-cookie":
                continue
            first, *attrs = [p.strip() for p in header.split(";")]
            name, _, value = first.partition("=")
            found = {k.lower(): v for k, _, v in (a.partition("=") for a in attrs)}
            if found.get("max-age") == "0" or value in ("", '""'):
                self.jar.pop(name, None)
            else:
                self.jar[name] = (value, found.get("path") or "/")
        self.http.cookies.clear()

    # -- requests -------------------------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Origin": self.origin, **(kwargs.pop("headers", None) or {})}
        if self.csrf and method.upper() not in ("GET", "HEAD"):
            headers.setdefault(CSRF_HEADER, self.csrf)
        cookies = self._cookie_header(urlsplit(path).path or "/")
        if cookies:
            headers["Cookie"] = cookies
        resp = self.http.request(method, path, headers=headers, follow_redirects=False, **kwargs)
        self._keep(resp)
        return resp

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def navigate(self, path: str, limit: int = 10) -> httpx.Response:
        """GET ``path`` as a page navigation, following redirects on this origin."""
        resp = self.get(path, headers=NAVIGATE)
        for _ in range(limit):
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            location = resp.headers["location"]
            parts = urlsplit(location)
            if parts.netloc and f"{parts.scheme}://{parts.netloc}" != self.origin:
                return resp
            resp = self.get(
                parts.path + (f"?{parts.query}" if parts.query else ""), headers=NAVIGATE
            )
        return resp

    def submit(self, path: str, page: httpx.Response, **fields: Any) -> httpx.Response:
        """Post a form from ``page`` (its token), as the browser would."""
        return self.post(path, data={"csrf_token": form_token(page), **fields})

    def adopt_session(self) -> dict[str, Any]:
        """After a sign-in, read the session and send its CSRF token from now on."""
        resp = self.get("/auth/session", headers={"Accept": "application/json"})
        assert resp.status_code == 200, resp.text
        session: dict[str, Any] = resp.json()
        self.csrf = session["csrf_token"]
        return session

    # -- identity -------------------------------------------------------------------------

    def finish_challenge(self, resp: httpx.Response, secret: bytes | None) -> bytes | None:
        """Answer a second-factor challenge after a sign-in step: enrol, or verify with ``secret``.

        Returns the TOTP key (new on enrolment).
        """
        if resp.status_code != 303 or "/auth/challenge" not in resp.headers.get("location", ""):
            return secret
        page = self.navigate("/auth/challenge")
        assert page.status_code == 200, page.text[:2000]
        shown = _TOTP.search(page.text)
        if shown:
            text = shown.group(1)
            secret = base64.b32decode(text + "=" * (-len(text) % 8))
            done = self.submit("/auth/challenge/enrol", page, code=totp_code(secret))
            assert done.status_code in (200, 303), done.text[:2000]
            return secret
        assert secret, "a verify challenge needs the enrolled key"
        done = self.submit(
            "/auth/challenge/verify", page, code=totp_code(secret), kind="totp", next="/"
        )
        assert done.status_code == 303, done.text[:2000]
        return secret

    def sign_in(self, email: str, password: str, secret: bytes | None = None) -> bytes | None:
        """Sign in through the form (and its second factor); returns the TOTP key."""
        self.csrf = None  # a token bound to an ended session would be refused
        page = self.navigate("/auth/sign-in")
        assert page.status_code == 200, page.text[:2000]
        resp = self.submit("/auth/sign-in", page, email=email, password=password, next="/")
        assert resp.status_code == 303, resp.text[:2000]
        secret = self.finish_challenge(resp, secret)
        self.adopt_session()
        return secret

    # -- chat -----------------------------------------------------------------------------

    def events(self, path: str, params: dict[str, Any]) -> Iterator[Event]:
        """The server-sent events of ``path``, parsed, until the stream ends."""
        headers = {"Origin": self.origin, "Accept": "text/event-stream"}
        cookies = self._cookie_header(path)
        if cookies:
            headers["Cookie"] = cookies
        with self.http.stream("GET", path, params=params, headers=headers) as resp:
            if resp.status_code != 200:
                resp.read()
                yield Event("http_error", {"status": resp.status_code, "body": resp.text})
                return
            name, data = "message", []
            for line in resp.iter_lines():
                if line.startswith(":"):
                    continue
                if not line:
                    if data:
                        raw = "\n".join(data)
                        try:
                            parsed: Any = json.loads(raw)
                        except json.JSONDecodeError:
                            parsed = raw
                        yield Event(name, parsed)
                        if name in ("complete", "error"):
                            return
                    name, data = "message", []
                elif line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())

    def chat(
        self, conversation_id: int, message: str, attachments: str | None = None
    ) -> list[Event]:
        params: dict[str, Any] = {"message": message, "conversation_id": conversation_id}
        if attachments:
            params["attachments"] = attachments
        return list(self.events("/stream/chat", params))

    def new_conversation(self, model_id: str, name: str = "Live e2e") -> int:
        resp = self.post("/conversations/api/create", json={"name": name, "model_id": model_id})
        assert resp.status_code in (200, 201), resp.text
        body = resp.json()
        return int(body.get("conversation_id") or body.get("id") or body["conversation"]["id"])


def final_text(events: list[Event]) -> str:
    """The text of the final ``response`` event."""
    for event in reversed(events):
        if event.name == "response" and isinstance(event.data, dict):
            return str(event.data.get("content", ""))
    return ""


def named(events: list[Event], name: str) -> list[Event]:
    return [e for e in events if e.name == name]
