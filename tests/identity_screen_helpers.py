"""Test-only helpers for the identity screens: forms, TOTP codes and captured mail.

TOTP codes are computed with warden's own RFC 6238 implementation
(``warden.identity.totp``) from the key the enrolment screen shows.
"""

from __future__ import annotations

import base64
import html
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from warden.identity import totp

from spark.mode import DatabaseSettings, Dialect
from spark.web.security import CSRF_FORM_FIELD, CSRF_HEADER
from tests.identity_web_helpers import PASSWORD, PUBLIC, server_client, sign_in, write_config

NAVIGATE = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}
ORIGIN = {"Origin": PUBLIC}


def form_token(page: Any) -> str:
    """The csrf_token of the (first) form on a page."""
    match = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
    assert match, page.text
    return match.group(1)


def field(page: Any, name: str) -> str:
    """The value of a page's (first) hidden or text field ``name``."""
    match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', page.text)
    assert match, page.text
    return html.unescape(match.group(1))


def post_form(client: TestClient, path: str, page: Any, **fields: str) -> Any:
    """Post a form from ``page`` (its token) to ``path``, as a browser on the public origin."""
    return client.post(
        path,
        data={CSRF_FORM_FIELD: form_token(page), **fields},
        headers=ORIGIN,
        follow_redirects=False,
    )


def secret_on(page: Any) -> bytes:
    """The TOTP key an enrolment page shows, decoded."""
    match = re.search(r'<code id="totp-secret"[^>]*>([A-Z2-7]+)</code>', page.text)
    assert match, page.text
    text = match.group(1)
    return base64.b32decode(text + "=" * (-len(text) % 8))


def code(secret: bytes, steps_ahead: int = 0) -> str:
    """The TOTP code for now (plus ``steps_ahead`` 30-second steps)."""
    return totp.code_at(secret, totp.step_for(time.time()) + steps_ahead)


def recovery_codes_on(page: Any) -> list[str]:
    match = re.search(r'<pre id="recovery-codes"[^>]*>(.*?)</pre>', page.text, re.S)
    assert match, page.text
    return [line.strip() for line in html.unescape(match.group(1)).splitlines() if line.strip()]


def enrol_under_challenge(client: TestClient, email: str) -> tuple[bytes, list[str], Any]:
    """Sign in to an enrolment challenge, enrol, and return the key, codes and final page."""
    resp = sign_in(client, email, PASSWORD)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/challenge")
    page = client.get("/auth/challenge")
    assert page.status_code == 200, page.text
    secret = secret_on(page)
    done = post_form(client, "/auth/challenge/enrol", page, code=code(secret))
    assert done.status_code == 200, done.text
    return secret, recovery_codes_on(done), done


def verify(client: TestClient, value: str, kind: str = "totp", next_path: str = "/") -> Any:
    """Answer a verify challenge from its page."""
    page = client.get("/auth/challenge")
    assert page.status_code == 200, page.text
    return post_form(client, "/auth/challenge/verify", page, code=value, kind=kind, next=next_path)


def message_of(page: Any) -> str:
    """The text of a page's alert."""
    match = re.search(r'role="alert">\s*<i[^>]*></i>\s*(.*?)\s*</div>', page.text, re.S)
    return html.unescape(match.group(1)) if match else ""


def adopt_session(client: TestClient) -> None:
    """After a sign-in finished on a screen, send the session's CSRF token and the Origin."""
    session = client.get("/auth/session", headers={"Accept": "application/json"})
    assert session.status_code == 200, session.text
    client.headers.update({"Origin": PUBLIC, CSRF_HEADER: session.json()["csrf_token"]})


# -- Fixtures (import them into a test module) ------------------------------------------


@pytest.fixture
def server_db(pg_server: str, _pg_template: str) -> Iterator[DatabaseSettings]:
    """A PostgreSQL clone at head (warden's chain included) for one test."""
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=f"{base}/{name}")
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def mfa_client(tmp_path: Path, server_db: DatabaseSettings) -> Iterator[TestClient]:
    """A server whose policy requires a second factor for password sign-in."""
    config = write_config(tmp_path / "config.yaml", require_mfa=True)
    with server_client(config, server_db) as client:
        yield client


@pytest.fixture
def plain_client(tmp_path: Path, server_db: DatabaseSettings) -> Iterator[TestClient]:
    """A server whose policy does not require a second factor."""
    with server_client(write_config(tmp_path / "config.yaml"), server_db) as client:
        yield client

