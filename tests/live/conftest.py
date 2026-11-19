"""Fixtures for the live end-to-end suite (A18), run against the stack from ``deploy/live``.

Every test here is marked ``integration`` (CI deselects it) and skipped unless
``SPARK_LIVE_STACK`` names the ``live.json`` that ``deploy/live/stack.py
prepare`` wrote. The suite drives the running containers over HTTP as browsers
on the public origin would, reads mail from the mail catcher's API, inspects
PostgreSQL directly only to observe (locks, job rows), and runs ``docker`` only
against its own Compose project.

Users are synthetic (``example.com``). Each run invites fresh ones, so the
suite can run again on the same stack; the administrator's password and TOTP
key are kept in the sandbox (``people.json``), never in the repository.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from tests.live.client import Browser

STATE_ENV = "SPARK_LIVE_STACK"
_LINK = r"(http://127\.0\.0\.1:\d+{path}\?token=[A-Za-z0-9_\-\.%=]+)"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    skip = pytest.mark.skip(reason=f"{STATE_ENV} names no live stack (deploy/live/stack.py)")
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)
            if not os.environ.get(STATE_ENV):
                item.add_marker(skip)


@dataclass
class Person:
    email: str
    password: str
    browser: Browser
    secret: bytes | None
    session: dict[str, Any] = field(default_factory=dict)

    @property
    def guid(self) -> str:
        return str(self.session["user_guid"])

    @property
    def warden_id(self) -> str:
        return str(self.session["warden_user_id"])


class Stack:
    """The running stack: its URLs, its Compose project, its database and its mail."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.base_url: str = state["base_url"]
        self.web2_url: str = state["web2_url"]
        self.sandbox = Path(state["sandbox"])
        self.people_file = self.sandbox / "live" / "people.json"
        self.run = time.strftime("%H%M%S") + secrets.token_hex(2)

    # -- docker -----------------------------------------------------------------------

    def compose(self, *args: str, check: bool = True) -> str:
        done = subprocess.run(  # noqa: S603 - fixed argv, this stack's project only
            [*self.state["compose"], *args], capture_output=True, text=True, check=False
        )
        if check and done.returncode != 0:
            raise AssertionError(f"docker compose {' '.join(args)}: {done.stderr}")
        return done.stdout

    def docker(self, *args: str) -> str:
        """``docker ARGS`` on this project's containers; stdout and stderr (logs use both)."""
        done = subprocess.run(  # noqa: S603 - fixed argv, this project's containers only
            ["docker", *args], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stderr
        return (done.stdout + done.stderr).strip()

    def container(self, service: str, index: int = 1) -> str:
        return f"{self.state['project']}-{service}-{index}"

    def ip_of(self, container: str) -> str:
        return self.docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container
        )

    def started_at(self, container: str) -> str:
        return self.docker("inspect", "-f", "{{.State.StartedAt}}", container)

    # -- database (observation only) ----------------------------------------------------

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.state["postgres_url"], autocommit=True) as conn:
            return list(conn.execute(query, params).fetchall())

    # -- mail catcher -------------------------------------------------------------------

    def mail_link(self, to: str, path: str, after: float = 0.0, timeout: float = 60.0) -> str:
        """The newest link to ``path`` in mail to ``to`` received after ``after``."""
        pattern = re.compile(_LINK.format(path=re.escape(path)))
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.state["mail_api"], timeout=10) as api:
            while time.monotonic() < deadline:
                found = api.get("/api/v1/search", params={"query": f'to:"{to}"'}).json()
                for summary in found.get("messages") or []:
                    message = api.get(f"/api/v1/message/{summary['ID']}").json()
                    match = pattern.search(message.get("Text") or message.get("HTML") or "")
                    created = message.get("Date") or ""
                    if match and _epoch(created) >= after - 5:
                        return match.group(1)
                time.sleep(1)
        raise AssertionError(f"no mail to {to} with a {path} link")

    # -- model stub ---------------------------------------------------------------------

    def stub_requests(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = httpx.get(self.state["stub_url"] + "/_stub/requests").json()
        return found

    # -- people -------------------------------------------------------------------------

    def remembered(self) -> dict[str, Any]:
        if self.people_file.exists():
            known: dict[str, Any] = json.loads(self.people_file.read_text("utf-8"))
            return known
        return {}

    def remember(self, email: str, password: str, secret: bytes | None) -> None:
        known = self.remembered()
        known[email] = {
            "password": password,
            "totp": base64.b32encode(secret).decode() if secret else None,
        }
        self.people_file.write_text(json.dumps(known, indent=2), encoding="utf-8", newline="\n")


def _epoch(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def new_password() -> str:
    return "live-" + secrets.token_urlsafe(18) + "-dev-only"


@pytest.fixture(scope="session")
def stack() -> Stack:
    path = Path(os.environ[STATE_ENV])
    return Stack(json.loads(path.read_text("utf-8")))


def wait_for(check: Callable[[], Any], timeout: float, what: str, every: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"{what} within {timeout:.0f}s (last: {last!r})")


@pytest.fixture(scope="session")
def admin(stack: Stack) -> Iterator[Person]:
    """The first administrator (from ``spark identity create-admin``), signed in on web."""
    email = stack.state["admin_email"]
    known = stack.remembered().get(email, {})
    password = known.get("password") or stack.state["admin_password"]
    secret = base64.b32decode(known["totp"]) if known.get("totp") else None
    browser = Browser(stack.base_url)
    secret = browser.sign_in(email, password, secret)
    stack.remember(email, password, secret)
    person = Person(email, password, browser, secret, browser.adopt_session())
    yield person
    browser.close()


#: The connection every scripted chat uses: Ollama's API on the model stub (D69).
STUB_CONNECTION = "stub-ollama"
#: The stub's chat model, declared as a catalogue overlay (Ollama models are not shipped).
STUB_MODEL = "llama3-stub"
#: A stub model with a small context window, for compaction.
TINY_MODEL = "tiny-stub"
#: Every role a stub connection is assigned to, so every invited user may use it.
ALL_ROLES = ("administrator", "auditor", "power_user", "user")


def connection(
    admin: Person,
    connection_id: str,
    service: str,
    name: str,
    values: dict[str, Any],
    *,
    secrets: dict[str, str] | None = None,
    residency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a connection through the connections API (as the modal does), unless it exists."""
    found = admin.browser.get(f"/api/connections/{connection_id}")
    if found.status_code == 200:
        existing: dict[str, Any] = found.json()
        return existing
    body: dict[str, Any] = {
        "id": connection_id,
        "service": service,
        "name": name,
        "values": values,
        "secrets": secrets or {},
    }
    if residency is not None:
        body["residency"] = {"default": False, **residency}
    resp = admin.browser.post("/api/connections", json=body)
    assert resp.status_code == 201, resp.text
    got = admin.browser.get(f"/api/connections/{connection_id}")
    assert got.status_code == 200, got.text
    created: dict[str, Any] = got.json()
    return created


def declare(
    admin: Person,
    connection_id: str,
    model_id: str,
    *,
    context_window: int = 131072,
    output_limit: int = 8192,
    scope: str = "local",
) -> dict[str, Any]:
    """Declare a model the catalogue lacks as a SPARK overlay row (the models view's form)."""
    resp = admin.browser.post(
        f"/api/connections/{connection_id}/overlays",
        json={
            "model_id": model_id,
            "kind": "chat",
            "context_window": context_window,
            "output_limit": output_limit,
            "tools": True,
            "residency_scope": scope,
        },
    )
    assert resp.status_code == 200, resp.text
    view: dict[str, Any] = resp.json()
    return view


def assign(
    admin: Person,
    connection_id: str,
    *,
    role: str | None = None,
    user_guid: str | None = None,
    monthly_credits: int | None = None,
) -> dict[str, Any]:
    """Assign a connection to a role or a user, with an allowance (None: no limit)."""
    body: dict[str, Any] = {"role": role, "user_guid": user_guid}
    if monthly_credits is None:
        body["no_limit"] = True
    else:
        body["monthly_credits"] = monthly_credits
    resp = admin.browser.put(f"/api/connections/{connection_id}/assignments", json=body)
    assert resp.status_code == 200, resp.text
    found: dict[str, Any] = resp.json()
    return found


def offered(person: Person, base_url: str | None = None) -> list[dict[str, Any]]:
    """The models the person's picker lists (``/conversations/api/models``)."""
    browser = person.browser.on(base_url) if base_url else person.browser
    models: list[dict[str, Any]] = browser.get("/conversations/api/models").json()["models"]
    return models


@pytest.fixture(scope="session")
def configured(stack: Stack, admin: Person) -> None:
    """What every check relies on: the stub connection and its models, the mail catcher,
    tools. Idempotent, so the suite can run again on the same stack."""
    resp = admin.browser.post(
        "/settings/api/instance",
        json={
            "tool_permissions.auto_approve": True,
            "embedded_tools.agents.enabled": True,
            "embedded_tools.documents.enabled": True,
            "embedded_tools.tool_results.documents": "warn",
            "mail.smtp.host": "mail",
            "mail.smtp.port": 1025,
            "mail.smtp.tls": "starttls",
            "mail.smtp.sender_address": "spark@example.com",
            "mail.smtp.sender_name": "SPARK live e2e",
            "mail.smtp.username": "spark",
            "mail.smtp.password": "spark-live-mail-dev-only",
        },
    )
    assert resp.status_code == 200, resp.text

    stub = {"base_url": stack.state.get("stub_internal", "http://model-stub:11434")}
    # The administrator's natural order: add the connection, then declare its models on
    # it. Declaring the same entry again on a later run changes nothing.
    connection(admin, STUB_CONNECTION, "ollama", "Model stub (Ollama)", stub)
    declare(admin, STUB_CONNECTION, STUB_MODEL)
    declare(admin, STUB_CONNECTION, TINY_MODEL, context_window=4096, output_limit=1024)
    held = {
        a["role"]
        for a in admin.browser.get(f"/api/connections/{STUB_CONNECTION}/assignments").json()[
            "assignments"
        ]
    }
    for role in ALL_ROLES:
        if role not in held:
            assign(admin, STUB_CONNECTION, role=role)


def invite_and_accept(
    stack: Stack, admin: Person, email: str, *roles: str, base_url: str | None = None
) -> Person:
    """Invite ``email`` from the administration screen; accept from the mailed link."""
    page = admin.browser.navigate("/admin/invitations")
    assert page.status_code == 200, page.text[:2000]
    sent_at = time.time()
    resp = admin.browser.submit(
        "/admin/invitations",
        page,
        email=email,
        display_name=email.split("@")[0],
        method="password_and_sso",
        roles=list(roles),
    )
    assert resp.status_code == 200, resp.text[:2000]
    assert "The link is sent" in resp.text, "the invitation was not mailed"
    link = stack.mail_link(email, "/auth/invitation", after=sent_at)
    assert link.startswith(stack.base_url + "/auth/invitation?token=")

    browser = Browser(base_url or stack.base_url)
    accept = browser.navigate(link[len(stack.base_url) :])
    assert accept.status_code == 200, accept.text[:2000]
    token = re.search(r'name="token" value="([^"]+)"', accept.text)
    assert token, accept.text[:2000]
    password = new_password()
    done = browser.submit(
        "/auth/invitation", accept, token=token.group(1), password=password, confirm=password
    )
    assert done.status_code == 303, done.text[:2000]
    secret = browser.finish_challenge(done, None)
    session = browser.adopt_session()
    stack.remember(email, password, secret)
    return Person(email, password, browser, secret, session)


@pytest.fixture(scope="session")
def alice(stack: Stack, admin: Person, configured: None) -> Iterator[Person]:
    person = invite_and_accept(stack, admin, f"alice-{stack.run}@example.com", "user")
    yield person
    person.browser.close()


@pytest.fixture(scope="session")
def bob(stack: Stack, admin: Person, configured: None) -> Iterator[Person]:
    person = invite_and_accept(stack, admin, f"bob-{stack.run}@example.com", "user")
    yield person
    person.browser.close()


@pytest.fixture(scope="session")
def model_id(stack: Stack, admin: Person, configured: None) -> str:
    """``stub-ollama/llama3-stub``, once both replicas offer it."""
    wanted = f"{STUB_CONNECTION}/{STUB_MODEL}"

    def listed() -> bool:
        return all(
            any(m["id"] == wanted for m in offered(admin, url))
            for url in (stack.base_url, stack.web2_url)
        )

    wait_for(listed, 60, "the model stub offered on both replicas")
    return wanted
