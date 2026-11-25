"""Test-only helpers: a server-mode SPARK app signed in through warden.

The app is the real one (``create_app`` with ``attach_database`` and
``attach_identity``) on a PostgreSQL clone at head, warden's chain included.
Users are synthetic (example.com) and created through warden.

warden 0.2.0 asks for a second factor on every password sign-in (ruling CS14).
So that a test wanting a session gets one, :func:`make_user` gives each user a
TOTP factor through warden, and :func:`sign_in` answers the challenge from it
(or enrols one through the screens for a user made some other way), the codes
computed with warden's own RFC 6238 implementation. A test about the challenge
itself marks its client with :func:`leave_second_factor_to_test`: its users
start with no factor and :func:`sign_in` stops at the password's answer.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings
from warden.identity import PasswordChangeIssued, totp

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


def write_config(path: Path, **extra: Any) -> Path:
    """A server's config.yaml: warden's defaults, so a second factor is always required."""
    data: dict[str, Any] = {
        "server": {"public_base_url": PUBLIC},
        "interface": {"browser_heartbeat": {"enabled": False}},
        **extra,
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
    return path


def client_options() -> dict[str, Any]:
    # psycopg's async driver needs the Selector loop on Windows.
    return {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


# One secrets store per database (a clone's name is random), shared by its replicas.
_DEPLOYMENT_SECRETS: dict[str, MemorySecrets] = {}


@contextmanager
def server_client(
    config: Path, database: DatabaseSettings, secrets: MemorySecrets | None = None
) -> Iterator[TestClient]:
    """A TestClient on the public origin of a server-mode app with warden attached.

    Without ``secrets``, servers on the same database share one secrets store,
    as replicas of a deployment do (warden's TOTP key opens every user's factor).
    """
    if secrets is None:
        secrets = _DEPLOYMENT_SECRETS.setdefault(str(database.url), MemorySecrets())
    mode.configure(RuntimeSettings(Mode.SERVER, database))
    try:
        app = create_app(Ctx(config, secrets))  # type: ignore[arg-type]
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
    if not answers_second_factor(client):
        leave_second_factor_to_test(other)
    return other


# -- The second factor --------------------------------------------------------------------

_RAW = "spark_test_raw_second_factor"


def leave_second_factor_to_test(client: TestClient) -> None:
    """Users made on ``client`` start with no factor, and sign_in stops at the password."""
    setattr(client, _RAW, True)


def answers_second_factor(client: TestClient) -> bool:
    return not getattr(client, _RAW, False)


@dataclass
class Factor:
    """A user's TOTP key and recovery codes, as the helpers enrolled them."""

    secret: bytes
    recovery_codes: list[str] = field(default_factory=list)
    last_step: int = 0


# By warden user id (random), so a key never outlives its user's database.
FACTORS: dict[str, Factor] = {}


def secret_on(page: Any) -> bytes:
    """The TOTP key an enrolment page shows, decoded."""
    match = re.search(r'<code id="totp-secret"[^>]*>([A-Z2-7]+)</code>', page.text)
    assert match, page.text
    text = match.group(1)
    return base64.b32decode(text + "=" * (-len(text) % 8))


def recovery_codes_on(page: Any) -> list[str]:
    match = re.search(r'<pre id="recovery-codes"[^>]*>(.*?)</pre>', page.text, re.S)
    assert match, page.text
    return [line.strip() for line in html.unescape(match.group(1)).splitlines() if line.strip()]


def now_step() -> int:
    return totp.step_for(time.time())


# A code one step old is accepted inside warden's drift window; computed in the last
# seconds of a step it can be two steps old by the time warden checks it (a busy
# machine), so the helpers wait for the next step first.
_STEP_MARGIN = 3.0


def settled_step() -> int:
    """The current TOTP step, after waiting out the end of a step about to change."""
    left = totp.PERIOD - (time.time() % totp.PERIOD)
    if left < _STEP_MARGIN:
        time.sleep(left + 0.05)
    return now_step()


async def enrol_totp(identity: Any, email: str, password: str = PASSWORD) -> Factor:
    """Give a user with a password and no factor a TOTP factor, through warden's challenge.

    The session the enrolment signs in is ended at once. The code used is the
    previous step's (inside warden's drift window), so the next sign-in can use
    the current one.
    """
    result = await identity.sign_in(email, password)
    challenge = result.challenge
    assert challenge is not None and challenge.purpose == "enrol", result
    enrolment = await identity.begin_enrolment(challenge=challenge.token)
    step = settled_step() - 1
    codes, outcome = await identity.confirm_enrolment(
        totp.code_at(enrolment.secret, step), challenge=challenge.token
    )
    if outcome is not None and not isinstance(outcome, PasswordChangeIssued):
        await identity.sign_out(await identity.authenticate(outcome.access_token))
    user = await identity.users.get_by_email(email)
    assert user is not None
    factor = Factor(enrolment.secret, list(codes), step)
    FACTORS[str(user.id)] = factor
    return factor


def _user_id(client: TestClient, email: str) -> str | None:
    identity = runtime(client).identity

    async def find() -> Any:
        return await identity.users.get_by_email(email)

    user = client.portal.call(find)  # type: ignore[attr-defined]
    return str(user.id) if user is not None else None


def factor_of(client: TestClient, email: str) -> Factor | None:
    """The factor the helpers know for ``email``, if any."""
    user_id = _user_id(client, email)
    return FACTORS.get(user_id) if user_id is not None else None


def remember_factor(client: TestClient, email: str, factor: Factor) -> None:
    """Record a factor a test enrolled itself, so sign_in can answer with it."""
    user_id = _user_id(client, email)
    assert user_id is not None
    FACTORS[user_id] = factor


def second_factor_answer(factor: Factor) -> tuple[str, str]:
    """The next code warden will take: a TOTP code in its window, else a recovery code."""
    step = max(settled_step() - 1, factor.last_step + 1)
    if step <= now_step() + 1:
        factor.last_step = step
        return "totp", totp.code_at(factor.secret, step)
    assert factor.recovery_codes, "no TOTP step or recovery code left"
    return "recovery", factor.recovery_codes.pop()


def _challenge_form(client: TestClient, path: str, page: Any, **fields: str) -> Any:
    match = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
    assert match, page.text
    return client.post(
        path,
        data={CSRF_FORM_FIELD: match.group(1), **fields},
        headers={"Origin": PUBLIC},
        follow_redirects=False,
    )


def answer_challenge(client: TestClient, email: str, password: str, resp: Any) -> Any:
    """Finish a sign-in warden answered with a challenge, through the challenge screens.

    A ``verify`` challenge is answered from the user's factor; an ``enrol``
    challenge (a user made without one) enrols TOTP, and the user then signs in
    again, so the answer is always what a sign-in with a second factor gives:
    303 to ``next`` with the session cookies, or to a password change.
    """
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    next_path = query.get("next", ["/"])[0]
    page = client.get("/auth/challenge")
    assert page.status_code == 200, page.text
    if 'id="totp-secret"' in page.text:
        secret = secret_on(page)
        step = settled_step() - 1
        done = _challenge_form(
            client,
            "/auth/challenge/enrol",
            page,
            code=totp.code_at(secret, step),
            next=next_path,
        )
        assert done.status_code == 200, done.text
        remember_factor(client, email, Factor(secret, recovery_codes_on(done), step))
        if "/auth/challenge?" in done.text:  # a password change comes next
            return done
        client.cookies.clear()
        return sign_in(client, email, password, next_path=next_path)
    factor = factor_of(client, email)
    assert factor is not None, f"{email} holds a factor the helpers did not enrol"
    kind, value = second_factor_answer(factor)
    return _challenge_form(
        client, "/auth/challenge/verify", page, code=value, kind=kind, next=next_path
    )


def runtime(client: TestClient) -> Any:
    return client.app.state.identity_runtime  # type: ignore[attr-defined]


def make_user(client: TestClient, email: str, *roles: str) -> uuid.UUID:
    """A synthetic active user with a password and ``roles`` (USER by default).

    The user holds a TOTP factor unless the client leaves the second factor to
    the test (:func:`leave_second_factor_to_test`).
    """
    identity = runtime(client).identity
    with_factor = answers_second_factor(client)

    async def create() -> uuid.UUID:
        user = await identity.users.create(email, actor="test", password=PASSWORD)
        for role in roles or (USER,):
            await identity.roles.assign(user.id, role, actor="test")
        if with_factor:
            await enrol_totp(identity, email)
        return user.id

    result: uuid.UUID = client.portal.call(create)  # type: ignore[attr-defined]
    return result


def sign_in_form_token(client: TestClient) -> str:
    page = client.get("/auth/sign-in")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, page.text
    return match.group(1)


def password_step(
    client: TestClient, email: str, password: str = PASSWORD, *, next_path: str = "/"
) -> Any:
    """Post the sign-in form alone: warden's answer to the password, as the browser sees it."""
    token = sign_in_form_token(client)
    return client.post(
        "/auth/sign-in",
        data={"email": email, "password": password, "next": next_path, CSRF_FORM_FIELD: token},
        headers={"Origin": PUBLIC},
        follow_redirects=False,
    )


def sign_in(
    client: TestClient, email: str, password: str = PASSWORD, *, next_path: str = "/"
) -> Any:
    """Sign in through the form, as a browser on the public origin would.

    The second factor warden asks for is answered through the challenge screens
    (:func:`answer_challenge`), unless the client leaves it to the test, when
    this returns the password's answer. Afterwards a signed-in client sends the
    Origin header and the session's CSRF token. Returns the last response:
    303 to ``next`` with the session cookies on success.
    """
    resp = password_step(client, email, password, next_path=next_path)
    if (
        answers_second_factor(client)
        and resp.status_code == 303
        and resp.headers["location"].startswith("/auth/challenge")
        and "reason=" not in resp.headers["location"]
    ):
        resp = answer_challenge(client, email, password, resp)
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
