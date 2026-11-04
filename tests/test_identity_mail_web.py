"""Server mode, through the real app: warden's mail over the ``mail.smtp`` instance settings.

The app runs on a PostgreSQL clone at head with warden attached; users are
synthetic and signed in through warden. Mail goes to
:class:`tests.fake_smtp.FakeSmtp` on 127.0.0.1 (plain text, which only a
loopback host allows). With ``mail.smtp.host`` set, an administrator's
invitation, resend and password reset are mailed and no longer shown; a
signed-out reset request is mailed and keeps its neutral answer; an enrolled
second factor is notified. Administrators alone change the settings and send a
test message to their own address; both are audited without a value.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.audit import AuditQuery, list_events
from warden.audit.store import AuditRow

from spark.identity import audit as spark_audit
from spark.identity.db import warden_database
from spark.identity.permissions import ADMINISTRATOR
from spark.mode import DatabaseSettings
from spark.web.endpoints.lifecycle import RESET_REQUESTED
from tests.fake_smtp import PASSWORD, USERNAME, FakeSmtp
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    adopt_session,
    enrol_under_challenge,
    message_of,
    post_form,
    server_db,
)
from tests.identity_web_helpers import (
    PUBLIC,
    another_client,
    make_user,
    server_client,
    sign_in,
    write_config,
)
from tests.test_instance_settings_web import MemorySecrets

pytestmark = pytest.mark.postgres

ROOT = "root@example.com"
ADA = "ada@example.com"
GRACE = "grace@example.com"
SENDER = "noreply@example.com"
WRONG = "wrong-dev-only"
FETCH = {"Accept": "application/json"}


@pytest.fixture
def secrets() -> MemorySecrets:
    return MemorySecrets()


@pytest.fixture
def client(
    tmp_path: Path, server_db: DatabaseSettings, secrets: MemorySecrets
) -> Iterator[TestClient]:
    with server_client(write_config(tmp_path / "config.yaml"), server_db, secrets) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    make_user(client, ROOT, ADMINISTRATOR)
    browser = another_client(client)
    assert sign_in(browser, ROOT).status_code == 303
    return browser


@pytest.fixture
def smtp() -> Iterator[FakeSmtp]:
    with FakeSmtp("none") as server:
        yield server


def _configure(admin: TestClient, port: int, **extra: Any) -> None:
    resp = admin.post(
        "/settings/api/instance",
        json={
            "mail.smtp.host": "127.0.0.1",
            "mail.smtp.port": port,
            "mail.smtp.tls": "none",
            "mail.smtp.sender_address": SENDER,
            "mail.smtp.sender_name": "Spark",
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text


def _wait_for(server: FakeSmtp, count: int, seconds: float = 10) -> None:
    """Mail sent after a response (a signed-out reset) arrives a moment later."""
    deadline = time.monotonic() + seconds
    while len(server.received) < count and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(server.received) >= count, server.received


def _rows(client: TestClient, action: str) -> list[AuditRow]:
    app: Any = client.app

    async def read() -> list[AuditRow]:
        audit = spark_audit.current()
        if audit is not None:
            await audit.drain()
        page = await list_events(warden_database(app.state.database), AuditQuery(actions=(action,)))
        return page.rows

    rows: list[AuditRow] = client.portal.call(read)  # type: ignore[attr-defined]
    return rows


def _user_link(server: FakeSmtp, path: str) -> str:
    text = server.last().get_body(("plain",))
    assert text is not None
    content = text.get_content()
    start = content.index(PUBLIC + path)
    return content[start:].split()[0][len(PUBLIC) :]


# -- Mailed links -------------------------------------------------------------------------


class TestMailedLinks:
    def test_an_invitation_is_mailed_and_not_shown(self, admin: TestClient, smtp: FakeSmtp) -> None:
        _configure(admin, smtp.port)
        page = admin.get("/admin/invitations")
        resp = post_form(admin, "/admin/invitations", page, email=GRACE, display_name="Grace")
        assert resp.status_code == 200, resp.text
        assert "The link is sent." in resp.text
        assert "/auth/invitation?token=" not in resp.text
        [received] = smtp.received
        assert received.recipients == [GRACE]
        message = smtp.last()
        assert message["From"] == f"Spark <{SENDER}>"
        link = _user_link(smtp, "/auth/invitation")
        assert another_client(admin).get(link).status_code == 200

    def test_a_resend_is_mailed(self, admin: TestClient, smtp: FakeSmtp) -> None:
        _configure(admin, smtp.port)
        page = admin.get("/admin/invitations")
        post_form(admin, "/admin/invitations", page, email=GRACE)
        invitations = admin.app.state.identity_runtime.identity.invitations  # type: ignore[attr-defined]
        [invitation] = admin.portal.call(invitations.pending)  # type: ignore[attr-defined]
        page = admin.get("/admin/invitations")
        resp = post_form(admin, f"/admin/invitations/{invitation.user.id}/resend", page)
        assert resp.status_code == 200 and "The link is sent." in resp.text
        assert "/auth/invitation?token=" not in resp.text
        assert [r.recipients for r in smtp.received] == [[GRACE], [GRACE]]

    def test_an_administrator_reset_is_mailed(self, admin: TestClient, smtp: FakeSmtp) -> None:
        _configure(admin, smtp.port)
        ada = make_user(admin, ADA)
        page = admin.get(f"/admin/users/{ada}")
        resp = post_form(admin, f"/admin/users/{ada}/password-reset", page)
        assert resp.status_code == 200 and "The link is sent." in resp.text
        assert "/auth/reset?token=" not in resp.text
        assert smtp.received[-1].recipients == [ADA]
        assert _user_link(smtp, "/auth/reset").startswith("/auth/reset?token=")

    def test_a_signed_out_reset_request_is_mailed_with_the_neutral_answer(
        self, admin: TestClient, smtp: FakeSmtp
    ) -> None:
        _configure(admin, smtp.port)
        make_user(admin, ADA)
        stranger = another_client(admin)
        stranger.cookies.clear()
        stranger.headers.clear()
        for email in (ADA, "nobody@example.com"):
            page = stranger.get("/auth/password-reset")
            resp = post_form(stranger, "/auth/password-reset", page, email=email)
            assert resp.status_code == 200 and RESET_REQUESTED in resp.text
            assert "/auth/reset?token=" not in resp.text
        _wait_for(smtp, 1)
        time.sleep(0.5)
        assert [r.recipients for r in smtp.received] == [[ADA]]

    def test_an_enrolled_second_factor_is_notified(
        self, tmp_path: Path, server_db: DatabaseSettings, smtp: FakeSmtp
    ) -> None:
        config = write_config(tmp_path / "mfa.yaml", require_mfa=True)
        with server_client(config, server_db) as client:
            make_user(client, ROOT, ADMINISTRATOR)
            root = another_client(client)
            enrol_under_challenge(root, ROOT)
            adopt_session(root)
            _configure(root, smtp.port)
            make_user(client, ADA)
            enrol_under_challenge(another_client(client), ADA)
            _wait_for(smtp, 1)
        assert smtp.received[-1].recipients == [ADA]
        assert smtp.last()["Subject"] == "Two-step verification was set up"


class TestFailures:
    @pytest.fixture
    def refusing(self) -> Iterator[FakeSmtp]:
        with FakeSmtp("none", credentials=(USERNAME, PASSWORD)) as server:
            yield server

    def test_an_invitation_that_cannot_be_mailed_shows_no_link(
        self, admin: TestClient, refusing: FakeSmtp
    ) -> None:
        _configure(
            admin, refusing.port, **{"mail.smtp.username": USERNAME, "mail.smtp.password": WRONG}
        )
        page = admin.get("/admin/invitations")
        resp = post_form(admin, "/admin/invitations", page, email=GRACE)
        assert resp.status_code == 502
        assert message_of(resp) == "The invitation could not be sent."
        assert "/auth/invitation?token=" not in resp.text and WRONG not in resp.text
        assert refusing.received == []

    def test_a_signed_out_reset_that_cannot_be_mailed_keeps_its_answer(
        self, admin: TestClient, refusing: FakeSmtp
    ) -> None:
        _configure(
            admin, refusing.port, **{"mail.smtp.username": USERNAME, "mail.smtp.password": WRONG}
        )
        make_user(admin, ADA)
        stranger = another_client(admin)
        stranger.cookies.clear()
        stranger.headers.clear()
        page = stranger.get("/auth/password-reset")
        resp = post_form(stranger, "/auth/password-reset", page, email=ADA)
        assert resp.status_code == 200 and RESET_REQUESTED in resp.text
        deadline = time.monotonic() + 10
        while not refusing.sign_ins and time.monotonic() < deadline:
            time.sleep(0.05)
        # smtplib tries each mechanism offered (PLAIN, then LOGIN); each is refused.
        assert refusing.sign_ins and not any(s.accepted for s in refusing.sign_ins)
        assert refusing.received == []

    def test_without_a_host_the_link_is_shown_once_as_before(
        self, admin: TestClient, smtp: FakeSmtp
    ) -> None:
        page = admin.get("/admin/invitations")
        resp = post_form(admin, "/admin/invitations", page, email=GRACE)
        assert resp.status_code == 200
        assert f"{PUBLIC}/auth/invitation?token=" in resp.text
        assert smtp.connections == 0


# -- The settings -------------------------------------------------------------------------


def _stored(client: TestClient) -> dict[str, Any]:
    """The instance_settings rows, values decoded (they are stored as JSON)."""
    sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
    rows = sync_db.fetch_all("SELECT key, value FROM instance_settings")
    return {row["key"]: json.loads(row["value"]) for row in rows}


class TestSettings:
    def test_an_administrator_saves_mail_smtp_with_the_password_in_the_secrets_store(
        self, admin: TestClient, secrets: MemorySecrets
    ) -> None:
        resp = admin.post(
            "/settings/api/instance",
            json={
                "mail.smtp.host": "smtp.example.com",
                "mail.smtp.port": "465",
                "mail.smtp.tls": "implicit",
                "mail.smtp.sender_address": SENDER,
                "mail.smtp.sender_name": "Spark",
                "mail.smtp.username": USERNAME,
                "mail.smtp.password": PASSWORD,
            },
        )
        assert resp.status_code == 200, resp.text
        assert secrets.get("mail_smtp_password") == PASSWORD
        stored = _stored(admin)
        assert "mail.smtp.password" not in stored
        assert PASSWORD not in repr(stored)
        assert admin.get("/settings/api/secrets").json()["mail.smtp.password"] is True
        page = admin.get("/settings")
        assert 'id="section-mail"' in page.text
        assert 'data-key="mail.smtp.host"' in page.text and "smtp.example.com" in page.text
        assert PASSWORD not in page.text

    def test_the_changes_are_audited_without_their_values(
        self, admin: TestClient, client: TestClient
    ) -> None:
        _configure(admin, 2525, **{"mail.smtp.password": PASSWORD})
        changed = {r.resource_id for r in _rows(client, "spark.settings.instance.changed")}
        assert {"mail.smtp.host", "mail.smtp.port", "mail.smtp.sender_address"} <= changed
        [secret] = _rows(client, "spark.settings.secret.changed")
        assert secret.resource_id == "mail.smtp.password"
        for row in _rows(client, "spark.settings.instance.changed") + [secret]:
            assert PASSWORD not in repr(row.details) and SENDER not in repr(row.details)

    def test_plain_text_to_another_host_is_refused_and_nothing_written(
        self, admin: TestClient, secrets: MemorySecrets
    ) -> None:
        resp = admin.post(
            "/settings/api/instance",
            json={
                "mail.smtp.host": "smtp.example.com",
                "mail.smtp.tls": "none",
                "mail.smtp.sender_address": SENDER,
                "mail.smtp.password": PASSWORD,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["keys"] == ["mail.smtp.tls"]
        assert "loopback" in resp.json()["detail"]
        assert not any(k.startswith("mail.smtp.") for k in _stored(admin))
        assert secrets.get("mail_smtp_password") is None

    def test_the_combination_includes_what_is_already_saved(self, admin: TestClient) -> None:
        _configure(admin, 2525)
        resp = admin.post("/settings/api/instance", json={"mail.smtp.host": "smtp.example.com"})
        assert resp.status_code == 400 and resp.json()["keys"] == ["mail.smtp.tls"]
        assert _stored(admin)["mail.smtp.host"] == "127.0.0.1"
        ok = admin.post(
            "/settings/api/instance",
            json={"mail.smtp.host": "smtp.example.com", "mail.smtp.tls": "starttls"},
        )
        assert ok.status_code == 200, ok.text

    def test_a_host_needs_a_sender_address(self, admin: TestClient) -> None:
        resp = admin.post("/settings/api/instance", json={"mail.smtp.host": "smtp.example.com"})
        assert resp.status_code == 400
        assert resp.json()["keys"] == ["mail.smtp.sender_address"]

    def test_a_user_cannot_change_mail_settings(
        self, client: TestClient, secrets: MemorySecrets
    ) -> None:
        make_user(client, ADA)
        ada = another_client(client)
        assert sign_in(ada, ADA).status_code == 303
        resp = ada.post(
            "/settings/api/instance",
            json={"mail.smtp.host": "127.0.0.1", "mail.smtp.password": PASSWORD},
        )
        assert resp.status_code == 403
        assert _stored(client) == {} and secrets.get("mail_smtp_password") is None


# -- The test message ---------------------------------------------------------------------


class TestTestMail:
    def test_an_administrator_sends_a_test_message_to_their_own_address(
        self, admin: TestClient, client: TestClient, smtp: FakeSmtp
    ) -> None:
        _configure(admin, smtp.port)
        resp = admin.post("/settings/api/mail/test", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "to": ROOT}
        [received] = smtp.received
        assert received.recipients == [ROOT]
        assert smtp.last()["Subject"] == "Spark test message"
        [row] = _rows(client, "spark.mail.test_sent")
        assert row.outcome == "success" and row.resource_id == "mail.smtp"
        assert ROOT not in repr(row.details)

    def test_a_failure_says_why_without_the_credential(
        self, admin: TestClient, client: TestClient
    ) -> None:
        with FakeSmtp("none", credentials=(USERNAME, PASSWORD)) as server:
            _configure(
                admin, server.port, **{"mail.smtp.username": USERNAME, "mail.smtp.password": WRONG}
            )
            resp = admin.post("/settings/api/mail/test", json={})
        assert resp.status_code == 502
        assert resp.json() == {
            "status": "error",
            "reason": "the mail server refused the username or password",
        }
        assert WRONG not in resp.text and PASSWORD not in resp.text
        [row] = _rows(client, "spark.mail.test_sent")
        assert row.outcome == "failure"
        assert row.details["reason"] == "the mail server refused the username or password"
        assert WRONG not in repr(row.details)

    def test_without_a_host_there_is_nothing_to_test(self, admin: TestClient) -> None:
        resp = admin.post("/settings/api/mail/test", json={})
        assert resp.status_code == 409
        assert resp.json()["status"] == "error"

    def test_a_user_cannot_send_one(self, admin: TestClient, smtp: FakeSmtp) -> None:
        _configure(admin, smtp.port)
        make_user(admin, ADA)
        ada = another_client(admin)
        assert sign_in(ada, ADA).status_code == 303
        resp = ada.post("/settings/api/mail/test", json={})
        assert resp.status_code == 403
        assert smtp.connections == 0

    def test_signed_out_cannot_send_one(self, admin: TestClient, smtp: FakeSmtp) -> None:
        _configure(admin, smtp.port)
        stranger = TestClient(admin.app, base_url=PUBLIC)
        stranger.portal = admin.portal  # type: ignore[attr-defined]
        resp = stranger.post("/settings/api/mail/test", json={}, headers={"Origin": PUBLIC})
        assert resp.status_code in (401, 403)
        assert smtp.connections == 0
