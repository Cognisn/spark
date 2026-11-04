"""warden's mail over SMTP on a server (spark.identity.mail), against a fake SMTP server.

Every delivery here goes to :class:`tests.fake_smtp.FakeSmtp` on 127.0.0.1:
STARTTLS and implicit TLS with a test authority, plain text only on loopback.
The ``mail.smtp`` settings are read through the settings accessor at each
send, and the password through the secrets store, so an unset host leaves
:class:`~spark.identity.mail.LinkCapture` in charge exactly as before. No
link, token or credential reaches a log line or a refusal's reason.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.identity import MailKind, MailMessage

from spark import mode
from spark.identity import mail
from spark.identity.mail import (
    InstanceMail,
    LinkCapture,
    SmtpRefused,
    SmtpSender,
    SmtpSettings,
    captured,
    is_loopback,
    settings_problem,
    smtp_settings,
)
from spark.instance_settings import accessor, registry
from spark.instance_settings.registry import InvalidSettingError, Scope
from spark.mode import Mode, RuntimeSettings
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.fake_smtp import PASSWORD, USERNAME, Certificates, FakeSmtp, make_certificates
from tests.test_instance_settings_web import Ctx, MemorySecrets
from tests.web_helpers import sign_in

TOKEN = "tok-dev-only-3f9a1c"
LINK = f"https://spark.example.com/auth/invitation?token={TOKEN}"
SENDER = "noreply@example.com"
ADA = "ada@example.com"


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Certificates:
    return make_certificates(Path(tmp_path_factory.mktemp("smtp-certs")))


def _message(kind: MailKind = MailKind.INVITATION) -> MailMessage:
    return MailMessage(
        kind=kind,
        to=ADA,
        subject="You have been invited",
        text=f"Hello Ada,\n\nAccept the invitation here:\n\n{LINK}\n",
        html=f'<p>Hello Ada,</p><p><a href="{LINK}">Accept the invitation</a></p>',
        from_address=None,
        from_name=None,
        reply_to="help@example.com",
    )


def _smtp(port: int, tls: str = "none", **extra: Any) -> SmtpSettings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "tls": tls,
        "sender_address": SENDER,
        "sender_name": "Spark",
        **extra,
    }
    return SmtpSettings(**values)


async def _send(sender: SmtpSender, message: MailMessage | None = None) -> None:
    await sender.send(message or _message())


class _Konfig:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def mail_settings() -> Iterator[Callable[..., None]]:
    """Install a settings source holding ``mail.smtp.<name>`` values (reset afterwards)."""

    def install(**values: Any) -> None:
        source = _Konfig({f"mail.smtp.{k}": v for k, v in values.items()})
        accessor.configure(accessor.FileSettings(source))

    yield install
    accessor.reset()


# -- The settings -------------------------------------------------------------------------


class TestRegistry:
    def test_mail_smtp_is_an_instance_group_with_its_password_in_the_secrets_store(
        self,
    ) -> None:
        keys = [k for k in registry.keys() if k.startswith("mail.smtp.")]
        assert keys == [
            "mail.smtp.host",
            "mail.smtp.port",
            "mail.smtp.tls",
            "mail.smtp.sender_address",
            "mail.smtp.sender_name",
            "mail.smtp.username",
            "mail.smtp.password",
        ]
        assert {registry.spec(k).scope for k in keys} == {Scope.INSTANCE}
        assert registry.secret_keys().count("mail.smtp.password") == 1
        assert registry.secret_name("mail.smtp.password") == "mail_smtp_password"
        assert registry.spec("mail.smtp.host").default == ""
        assert registry.spec("mail.smtp.port").default == 587
        assert registry.spec("mail.smtp.tls").default == "starttls"

    def test_tls_is_starttls_implicit_or_none(self) -> None:
        assert registry.spec("mail.smtp.tls").choices == ("starttls", "implicit", "none")
        with pytest.raises(InvalidSettingError):
            registry.coerce("mail.smtp.tls", "ssl")

    def test_the_port_is_a_tcp_port(self) -> None:
        assert registry.coerce("mail.smtp.port", "465") == 465
        for bad in ("0", "65536", "smtp"):
            with pytest.raises(InvalidSettingError):
                registry.coerce("mail.smtp.port", bad)


class TestLoopback:
    @pytest.mark.parametrize(
        "host", ["localhost", "LocalHost.", "127.0.0.1", "127.8.9.10", "::1", "[::1]"]
    )
    def test_loopback_hosts(self, host: str) -> None:
        assert is_loopback(host)

    @pytest.mark.parametrize(
        "host",
        ["smtp.example.com", "localhost.example.com", "0.0.0.0", "192.0.2.10", "::", ""],
    )
    def test_other_hosts(self, host: str) -> None:
        assert not is_loopback(host)


class TestSettingsProblem:
    def test_an_unset_host_is_never_a_problem(self) -> None:
        assert settings_problem({"mail.smtp.host": "", "mail.smtp.tls": "none"}) is None

    def test_plain_text_is_refused_unless_the_host_is_loopback(self) -> None:
        values = {
            "mail.smtp.host": "smtp.example.com",
            "mail.smtp.tls": "none",
            "mail.smtp.sender_address": SENDER,
        }
        problem = settings_problem(values)
        assert problem is not None and problem[0] == "mail.smtp.tls"
        assert "loopback" in problem[1]
        assert settings_problem({**values, "mail.smtp.host": "127.0.0.1"}) is None
        assert settings_problem({**values, "mail.smtp.tls": "starttls"}) is None

    def test_a_host_needs_a_sender_address(self) -> None:
        values = {"mail.smtp.host": "smtp.example.com", "mail.smtp.tls": "implicit"}
        assert settings_problem(values) == (
            "mail.smtp.sender_address",
            "a sender address is needed once a host is set",
        )
        for bad in ("postmaster", "ada@example.com\r\nBcc: x@example.com", "a b@example.com"):
            problem = settings_problem({**values, "mail.smtp.sender_address": bad})
            assert problem is not None and problem[0] == "mail.smtp.sender_address"
            assert bad not in problem[1]

    def test_names_and_hosts_hold_no_line_breaks(self) -> None:
        values = {"mail.smtp.host": "smtp.example.com", "mail.smtp.sender_address": SENDER}
        problem = settings_problem({**values, "mail.smtp.sender_name": "Spark\nBcc: x"})
        assert problem is not None and problem[0] == "mail.smtp.sender_name"
        problem = settings_problem({**values, "mail.smtp.host": "smtp.example.com\r\n"})
        assert problem is not None and problem[0] == "mail.smtp.host"


# -- Delivery -----------------------------------------------------------------------------


class TestDelivery:
    async def test_starttls_signs_in_over_tls_and_delivers_warden_s_message(
        self, certificates: Certificates
    ) -> None:
        with FakeSmtp("starttls", certificates, credentials=(USERNAME, PASSWORD)) as server:
            smtp = _smtp(server.port, "starttls", username=USERNAME, password=PASSWORD)
            await _send(SmtpSender(smtp, context=certificates.client_context()))
        [received] = server.received
        assert received.tls and received.user == USERNAME
        assert [(s.user, s.tls, s.accepted) for s in server.sign_ins] == [(USERNAME, True, True)]
        assert received.mail_from == SENDER and received.recipients == [ADA]
        message = received.message()
        assert message["From"] == f"Spark <{SENDER}>"
        assert message["To"] == ADA
        assert message["Subject"] == "You have been invited"
        assert message["Reply-To"] == "help@example.com"
        assert message["Message-ID"].endswith("@example.com>")
        assert message["Date"]
        body = message.get_body(("plain",))
        html = message.get_body(("html",))
        assert body is not None and LINK in body.get_content()
        assert html is not None and LINK in html.get_content()

    async def test_greets_with_the_sender_s_domain_not_this_machine_s_name(
        self, certificates: Certificates, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # socket.getfqdn() is a reverse lookup that can stall (macOS) and names this host.
        def no_lookup(*_: object) -> str:
            raise AssertionError("socket.getfqdn() was called")

        monkeypatch.setattr(socket, "getfqdn", no_lookup)
        for security in ("starttls", "implicit"):
            with FakeSmtp(security, certificates) as server:
                await _send(
                    SmtpSender(_smtp(server.port, security), context=certificates.client_context())
                )
            assert server.greetings and set(server.greetings) == {"example.com"}

    async def test_implicit_tls(self, certificates: Certificates) -> None:
        with FakeSmtp("implicit", certificates) as server:
            await _send(
                SmtpSender(_smtp(server.port, "implicit"), context=certificates.client_context())
            )
        [received] = server.received
        assert received.tls

    async def test_plain_text_to_a_loopback_relay(self) -> None:
        with FakeSmtp("none") as server:
            await _send(SmtpSender(_smtp(server.port, "none")))
        [received] = server.received
        assert not received.tls and received.recipients == [ADA]

    async def test_plain_text_to_another_host_is_refused_before_connecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts: list[Any] = []

        def connect(*args: Any, **kwargs: Any) -> Any:
            attempts.append(args)
            raise AssertionError("no connection may be made")

        monkeypatch.setattr(socket, "create_connection", connect)
        smtp = _smtp(25, "none", host="smtp.example.com")
        with pytest.raises(SmtpRefused) as refused:
            await _send(SmtpSender(smtp))
        assert "loopback" in refused.value.reason
        assert attempts == []

    async def test_a_server_that_does_not_offer_starttls_is_refused(
        self, certificates: Certificates
    ) -> None:
        with FakeSmtp(
            "starttls", certificates, advertise_starttls=False, credentials=(USERNAME, PASSWORD)
        ) as server:
            smtp = _smtp(server.port, "starttls", username=USERNAME, password=PASSWORD)
            with pytest.raises(SmtpRefused) as refused:
                await _send(SmtpSender(smtp, context=certificates.client_context()))
        assert refused.value.reason == "the mail server does not offer STARTTLS"
        assert server.sign_ins == [] and server.received == []

    async def test_an_untrusted_certificate_is_refused(self, certificates: Certificates) -> None:
        with FakeSmtp("starttls", certificates, credentials=(USERNAME, PASSWORD)) as server:
            smtp = _smtp(server.port, "starttls", username=USERNAME, password=PASSWORD)
            with pytest.raises(SmtpRefused) as refused:
                await _send(SmtpSender(smtp))  # the system's authorities only
        assert refused.value.reason == "the mail server's certificate could not be verified"
        assert server.sign_ins == [] and server.received == []

    async def test_a_refused_sign_in_says_so_without_the_credential(
        self, certificates: Certificates, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        with FakeSmtp("starttls", certificates, credentials=(USERNAME, PASSWORD)) as server:
            smtp = _smtp(server.port, "starttls", username=USERNAME, password="wrong-dev-only")
            with pytest.raises(SmtpRefused) as refused:
                await _send(SmtpSender(smtp, context=certificates.client_context()))
        assert refused.value.reason == "the mail server refused the username or password"
        assert "wrong-dev-only" not in str(refused.value) + repr(smtp) + caplog.text
        assert server.received == []

    async def test_a_server_that_never_answers_times_out(self) -> None:
        with FakeSmtp("none", silent=True) as server:
            started = time.monotonic()
            with pytest.raises(SmtpRefused) as refused:
                await _send(SmtpSender(_smtp(server.port), timeout=0.5))
            assert time.monotonic() - started < 5
        assert refused.value.reason == "the mail server did not answer in time"

    async def test_nothing_listening(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with pytest.raises(SmtpRefused) as refused:
            # Windows retries a refused loopback connection for about two seconds.
            await _send(SmtpSender(_smtp(port), timeout=10))
        assert refused.value.reason == "the mail server refused the connection"

    async def test_sending_happens_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        threads: list[int] = []

        def deliver(*args: Any, **kwargs: Any) -> None:
            threads.append(threading.get_ident())

        monkeypatch.setattr(mail, "_deliver", deliver)
        await _send(SmtpSender(_smtp(25)))
        assert threads and threads[0] != threading.get_ident()


# -- The sender warden is given -----------------------------------------------------------


class TestInstanceMail:
    async def test_without_a_host_link_capture_stays_the_sender(
        self, mail_settings: Callable[..., None]
    ) -> None:
        mail_settings(host="", sender_address=SENDER)
        sender = InstanceMail(MemorySecrets(), LinkCapture())
        with captured() as held:
            await sender.send(_message())
        assert held == [_message()]

    async def test_without_a_settings_source_link_capture_stays_the_sender(self) -> None:
        accessor.reset()
        sender = InstanceMail(MemorySecrets(), LinkCapture())
        with captured() as held:
            await sender.send(_message())
        assert held == [_message()]

    async def test_with_a_host_the_message_is_mailed_and_not_captured(
        self, mail_settings: Callable[..., None]
    ) -> None:
        secrets = MemorySecrets()
        secrets.set("mail_smtp_password", PASSWORD)
        with FakeSmtp("none", credentials=(USERNAME, PASSWORD)) as server:
            mail_settings(
                host="127.0.0.1",
                port=server.port,
                tls="none",
                sender_address=SENDER,
                username=USERNAME,
            )
            with captured() as held:
                await InstanceMail(secrets, LinkCapture()).send(_message())
        assert held == []
        [received] = server.received
        assert received.user == USERNAME and LINK in received.data.decode()

    async def test_settings_are_read_at_each_send(self, mail_settings: Callable[..., None]) -> None:
        sender = InstanceMail(MemorySecrets(), LinkCapture())
        mail_settings(host="")
        with captured() as held:
            await sender.send(_message())
        assert len(held) == 1
        with FakeSmtp("none") as server:
            mail_settings(host="127.0.0.1", port=server.port, tls="none", sender_address=SENDER)
            await sender.send(_message())
        assert len(server.received) == 1

    async def test_a_failed_delivery_raises_for_warden_to_report(
        self, mail_settings: Callable[..., None]
    ) -> None:
        mail_settings(host="smtp.example.com", tls="none", sender_address=SENDER)
        with pytest.raises(SmtpRefused):
            await InstanceMail(MemorySecrets(), LinkCapture()).send(_message())

    async def test_no_link_token_or_password_reaches_a_log_line(
        self, mail_settings: Callable[..., None], caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        secrets = MemorySecrets()
        secrets.set("mail_smtp_password", "wrong-dev-only")
        with FakeSmtp("none", credentials=(USERNAME, PASSWORD)) as server:
            mail_settings(
                host="127.0.0.1",
                port=server.port,
                tls="none",
                sender_address=SENDER,
                username=USERNAME,
            )
            with pytest.raises(SmtpRefused):
                await InstanceMail(secrets, LinkCapture()).send(_message())
            secrets.set("mail_smtp_password", PASSWORD)
            await InstanceMail(secrets, LinkCapture()).send(_message())
        assert len(server.received) == 1
        for forbidden in (TOKEN, LINK, "wrong-dev-only", PASSWORD):
            assert forbidden not in caplog.text

    def test_smtp_settings_are_none_without_a_host(
        self, mail_settings: Callable[..., None]
    ) -> None:
        mail_settings(host="  ")
        assert smtp_settings(MemorySecrets()) is None

    def test_smtp_settings_read_the_password_from_the_secrets_store(
        self, mail_settings: Callable[..., None]
    ) -> None:
        secrets = MemorySecrets()
        secrets.set("mail_smtp_password", PASSWORD)
        mail_settings(host="smtp.example.com", port="465", tls="implicit", sender_address=SENDER)
        found = smtp_settings(secrets)
        assert found is not None
        assert (found.host, found.port, found.tls, found.password) == (
            "smtp.example.com",
            465,
            "implicit",
            PASSWORD,
        )
        assert PASSWORD not in repr(found)


# -- Personal mode ------------------------------------------------------------------------


@pytest.fixture
def personal(tmp_path: Path, sqlite_settings: Any) -> Iterator[TestClient]:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8", newline="\n")
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    try:
        app = create_app(Ctx(config, MemorySecrets()))  # type: ignore[arg-type]
        with app_client(app, sqlite_settings) as client:
            sign_in(client)
            yield client
    finally:
        mode.reset()
        accessor.reset()


class TestPersonalMode:
    def test_the_settings_page_has_no_mail_section(self, personal: TestClient) -> None:
        page = personal.get("/settings")
        assert page.status_code == 200
        assert 'id="section-mail"' not in page.text
        assert 'data-key="mail.smtp.host"' not in page.text

    def test_there_is_no_test_message(self, personal: TestClient) -> None:
        assert personal.post("/settings/api/mail/test", json={}).status_code == 404

    def test_the_mail_password_is_not_a_personal_secret(self, personal: TestClient) -> None:
        status = personal.get("/settings/api/secrets").json()
        assert "providers.anthropic.api_key" in status
        assert not any(key.startswith("mail.smtp.") for key in status)

    def test_mail_settings_are_refused_and_nothing_written(
        self, personal: TestClient, tmp_path: Path
    ) -> None:
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        resp = personal.post(
            "/settings/api/save",
            json={"voice.engine": "browser", "mail.smtp.host": "smtp.example.com"},
        )
        assert resp.status_code == 400
        assert resp.json()["keys"] == ["mail.smtp.host"]
        assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before
