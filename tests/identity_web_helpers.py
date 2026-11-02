"""Test-only helpers: a server-mode SPARK app signed in through warden.

The app is the real one (``create_app`` with ``attach_database`` and
``attach_identity``) on a PostgreSQL clone at head, warden's chain included.
Users are synthetic (example.com) and created through warden.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

from spark import mode
from spark.identity.permissions import USER
from spark.mode import DatabaseSettings, Mode, RuntimeSettings
from spark.web.security import CSRF_FORM_FIELD, CSRF_HEADER
from spark.web.server import attach_database, attach_identity, create_app
from tests.test_instance_settings_web import MemorySecrets

PUBLIC = "https://spark.example.com"
PASSWORD = "correct-horse-battery-dev-only"


class Ctx:
    def __init__(self, config: Path, secrets: MemorySecrets) -> None:
        self.settings = Settings(
            config_file=config,
            defaults={"interface": {"session_timeout_minutes": 60}},
        )
        self.secrets = secrets


def write_config(path: Path, *, require_mfa: bool = False, **extra: Any) -> Path:
    data: dict[str, Any] = {
        "server": {"public_base_url": PUBLIC},
        "warden": {"identity": {"require_mfa": require_mfa}},
        "interface": {"browser_heartbeat": {"enabled": False}},
        **extra,
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
    return path


def client_options() -> dict[str, Any]:
    # psycopg's async driver needs the Selector loop on Windows.
    return {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


@contextmanager
def server_client(
    config: Path, database: DatabaseSettings, secrets: MemorySecrets | None = None
) -> Iterator[TestClient]:
    """A TestClient on the public origin of a server-mode app with warden attached."""
    mode.configure(RuntimeSettings(Mode.SERVER, database))
    try:
        app = create_app(Ctx(config, secrets or MemorySecrets()))  # type: ignore[arg-type]
        attach_database(app, database, True)
        attach_identity(app)
        with TestClient(app, base_url=PUBLIC, backend_options=client_options()) as client:
            yield client
    finally:
        mode.reset()


def another_client(client: TestClient) -> TestClient:
    """A second browser on the same running app (sharing its event loop)."""
    other = TestClient(client.app, base_url=PUBLIC)
    other.portal = client.portal
    return other


def runtime(client: TestClient) -> Any:
    return client.app.state.identity_runtime  # type: ignore[attr-defined]


def make_user(client: TestClient, email: str, *roles: str) -> uuid.UUID:
    """A synthetic active user with a password and ``roles`` (USER by default)."""
    identity = runtime(client).identity

    async def create() -> uuid.UUID:
        user = await identity.users.create(email, actor="test", password=PASSWORD)
        for role in roles or (USER,):
            await identity.roles.assign(user.id, role, actor="test")
        return user.id

    result: uuid.UUID = client.portal.call(create)  # type: ignore[attr-defined]
    return result


def sign_in_form_token(client: TestClient) -> str:
    page = client.get("/auth/sign-in")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, page.text
    return match.group(1)


def sign_in(client: TestClient, email: str, password: str = PASSWORD) -> Any:
    """Sign in through the form, as a browser on the public origin would.

    Afterwards the client sends the Origin header and the session's CSRF token.
    Returns the sign-in response.
    """
    token = sign_in_form_token(client)
    resp = client.post(
        "/auth/sign-in",
        data={"email": email, "password": password, CSRF_FORM_FIELD: token},
        headers={"Origin": PUBLIC},
        follow_redirects=False,
    )
    if resp.status_code == 303 and "/auth/challenge" not in resp.headers["location"]:
        session = client.get("/auth/session", headers={"Accept": "application/json"})
        client.headers.update({"Origin": PUBLIC, CSRF_HEADER: session.json()["csrf_token"]})
    return resp


def set_cookie_headers(resp: Any) -> list[str]:
    return [value for key, value in resp.headers.multi_items() if key.lower() == "set-cookie"]


def cookie_header(resp: Any, name: str) -> str:
    found = [h for h in set_cookie_headers(resp) if h.startswith(f"{name}=")]
    assert len(found) == 1, set_cookie_headers(resp)
    return found[0]


def attributes(header: str) -> dict[str, str]:
    """A Set-Cookie header's attributes, lower-cased names (flags map to "")."""
    parts = [p.strip() for p in header.split(";")[1:]]
    found: dict[str, str] = {}
    for part in parts:
        name, _, value = part.partition("=")
        found[name.lower()] = value
    return found


def claims(access_token: str) -> dict[str, Any]:
    """The (unverified) claims of a warden access token."""
    payload = access_token.split(".")[1]
    loaded: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    return loaded


def app_of(client: TestClient) -> FastAPI:
    app: FastAPI = client.app  # type: ignore[assignment]
    return app
