"""Signing a TestClient in to the personal-mode web interface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from spark.web.auth import LOGIN_CSRF_COOKIE, SESSION_COOKIE
from spark.web.security import CSRF_FORM_FIELD, CSRF_HEADER

ORIGIN = "http://testserver"


def login_token(client: TestClient) -> str:
    """Load the sign-in page and return the CSRF token its form carries."""
    client.get("/login")
    return client.cookies.get(LOGIN_CSRF_COOKIE) or ""


def sign_in(client: TestClient) -> dict[str, str]:
    """Sign in with a fresh code, as a browser on the instance's own origin would.

    Afterwards the client sends the session cookie, the Origin header and the
    session's CSRF token on every request. Returns the session cookie as a
    dict, for tests that pass cookies per request.
    """
    app = client.app
    code = app.state.auth.generate_code()  # type: ignore[attr-defined]
    resp = client.post(
        "/api/auth",
        data={"code": code, CSRF_FORM_FIELD: login_token(client)},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    session_id = resp.cookies.get(SESSION_COOKIE, "")
    client.cookies.set(SESSION_COOKIE, session_id)
    token = app.state.session.csrf_token(session_id) or ""  # type: ignore[attr-defined]
    client.headers.update({"Origin": ORIGIN, CSRF_HEADER: token})
    return {SESSION_COOKIE: session_id}
