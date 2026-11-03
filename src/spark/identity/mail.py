"""warden's mail in SPARK: delivered over SMTP when ``mail.smtp`` is set, links shown otherwise.

warden mails invitation and password reset links, and a notice when a second
factor is enrolled, through a ``MailSender`` the product supplies, and refuses
to invite anyone without one. A server's sender is :class:`InstanceMail`. At
each message it reads the ``mail.smtp`` instance settings (administrators edit
them on the instance page; every replica sees a change through the settings
listener) and the password from the secrets store (A9):

* with ``mail.smtp.host`` set, :class:`SmtpSender` delivers the message, off
  the event loop and within :data:`SEND_TIMEOUT`. STARTTLS or implicit TLS
  with the system's certificate authorities is required; plain text only to a
  loopback host (:func:`is_loopback`), such as a relay beside SPARK. A message
  that cannot be delivered raises :class:`SmtpRefused` with a fixed reason,
  which warden reports as ``MailDeliveryError``;
* with no host, :class:`LinkCapture` is the sender, as before. Inside
  :func:`captured`, a block an administrator's request opens around
  ``invitations.create``, ``invitations.resend`` or ``resets.request``, each
  message is kept for that block alone, so the screen can show the
  administrator the link once (:func:`link_in`). Anywhere else (a password
  reset asked for by someone signed out, warden's notice that a second factor
  was enrolled) the message is dropped. A reset request therefore keeps its
  neutral answer and no link leaves the server.

:func:`settings_problem` checks a combination of ``mail.smtp`` values before
they are saved; :func:`send_test_mail` sends an administrator's test message.
Nothing here logs a message, a link, a token, an address or a credential, and
no reason echoes a server's reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Protocol

from warden.identity import MailMessage, MailSender

from spark.instance_settings import accessor, registry

__all__ = [
    "MAIL_SMTP_KEYS",
    "SEND_TIMEOUT",
    "InstanceMail",
    "LinkCapture",
    "MailNotConfigured",
    "SecretReader",
    "SmtpRefused",
    "SmtpSender",
    "SmtpSettings",
    "build_test_mail",
    "captured",
    "is_loopback",
    "link_in",
    "send_test_mail",
    "settings_problem",
    "smtp_settings",
    "tls_context",
]

logger = logging.getLogger(__name__)

_CAPTURE: contextvars.ContextVar[list[MailMessage] | None] = contextvars.ContextVar(
    "spark_identity_mail_capture", default=None
)


class LinkCapture:
    """A ``MailSender`` that hands messages to the open :func:`captured` block, or drops them."""

    async def send(self, message: MailMessage) -> None:
        held = _CAPTURE.get()
        if held is None:
            logger.info("No mail service: a %s message was not sent", message.kind.value)
            return
        held.append(message)


@contextlib.contextmanager
def captured() -> Iterator[list[MailMessage]]:
    """Keep the messages warden sends during the block (this task only) in the list yielded."""
    held: list[MailMessage] = []
    token = _CAPTURE.set(held)
    try:
        yield held
    finally:
        _CAPTURE.reset(token)


def link_in(messages: list[MailMessage], base_url: str, path: str) -> str | None:
    """The link to ``base_url`` + ``path`` in the last captured message, or None."""
    pattern = re.compile(re.escape(base_url.rstrip("/") + path) + r"\?token=[A-Za-z0-9_\-]+")
    for message in reversed(messages):
        found = pattern.search(message.text)
        if found is not None:
            return found.group(0)
    return None


# -- The mail.smtp settings -----------------------------------------------------------------

HOST = "mail.smtp.host"
PORT = "mail.smtp.port"
TLS = "mail.smtp.tls"
SENDER_ADDRESS = "mail.smtp.sender_address"
SENDER_NAME = "mail.smtp.sender_name"
USERNAME = "mail.smtp.username"
PASSWORD = "mail.smtp.password"  # noqa: S105 - a setting's key, not a password
MAIL_SMTP_KEYS = (HOST, PORT, TLS, SENDER_ADDRESS, SENDER_NAME, USERNAME, PASSWORD)

# Seconds a message (connection, TLS, sign-in and sending) may take.
SEND_TIMEOUT = 30.0

_ADDRESS = re.compile(r"[^@\s<>,;\"()]+@[^@\s<>,;\"()]+\.[^@\s<>,;\"()]+")


class SecretReader(Protocol):
    """The part of konfig's secrets store the sender reads."""

    def get(self, key: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """One ``mail.smtp`` configuration; the password never appears in its repr."""

    host: str
    port: int
    tls: str
    sender_address: str
    sender_name: str = ""
    username: str = ""
    password: str = field(default="", repr=False)


def is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine: ``localhost`` or a loopback address (no lookup)."""
    name = host.strip().lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


def _has_line_break(text: str) -> bool:
    return any(ch in text for ch in "\r\n\x00")


def settings_problem(values: Mapping[str, Any]) -> tuple[str, str] | None:
    """The first ``(key, reason)`` this ``mail.smtp`` combination is refused for, or None.

    ``values`` holds the settings as they would be once saved. No reason
    contains a value.
    """
    host = str(values.get(HOST) or "")
    if not host.strip():
        return None
    if _has_line_break(host) or any(ch.isspace() for ch in host.strip()):
        return HOST, "a host name holds no spaces or line breaks"
    if values.get(TLS) == "none" and not is_loopback(host):
        return TLS, "TLS is required unless the host is loopback (localhost, 127.0.0.1 or ::1)"
    address = str(values.get(SENDER_ADDRESS) or "")
    if not address.strip():
        return SENDER_ADDRESS, "a sender address is needed once a host is set"
    if not _ADDRESS.fullmatch(address.strip()):
        return SENDER_ADDRESS, "expected one email address, such as noreply@example.com"
    for key in (SENDER_NAME, USERNAME):
        if _has_line_break(str(values.get(key) or "")):
            return key, "this setting holds no line breaks"
    return None


def smtp_settings(secrets: SecretReader | None) -> SmtpSettings | None:
    """The current ``mail.smtp`` configuration, or None when no host is set.

    Reads the settings source the process installed (None when it has none)
    and the password from the secrets store.
    """
    try:
        source = accessor.current()
    except RuntimeError:
        return None
    host = str(source.get(HOST) or "").strip()
    if not host:
        return None
    password = secrets.get(registry.secret_name(PASSWORD)) if secrets is not None else None
    return SmtpSettings(
        host=host,
        port=int(source.get(PORT)),
        tls=str(source.get(TLS)),
        sender_address=str(source.get(SENDER_ADDRESS) or "").strip(),
        sender_name=str(source.get(SENDER_NAME) or "").strip(),
        username=str(source.get(USERNAME) or "").strip(),
        password=password or "",
    )


# -- Delivery -------------------------------------------------------------------------------


class MailNotConfigured(Exception):
    """``mail.smtp.host`` is not set."""


class SmtpRefused(Exception):
    """A message was not delivered; ``reason`` says why without a credential or reply."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_PLAIN_REFUSED = "TLS is required unless the host is loopback"


def tls_context() -> ssl.SSLContext:
    """The client TLS context: the system's authorities, host names checked, TLS 1.2 or later."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _reason(exc: BaseException) -> str:
    """A fixed reason for a failed delivery (never the exception's text)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the mail server refused the username or password"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "the mail server does not offer STARTTLS"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "the mail server refused the recipient"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "the mail server refused the sender address"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "the mail server closed the connection"
    if isinstance(exc, smtplib.SMTPException):
        return "the mail server refused the message"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "the mail server's certificate could not be verified"
    if isinstance(exc, ssl.SSLError):
        return "TLS could not be negotiated with the mail server"
    if isinstance(exc, TimeoutError):
        return "the mail server did not answer in time"
    if isinstance(exc, socket.gaierror):
        return "the mail server's host name could not be resolved"
    if isinstance(exc, ConnectionRefusedError):
        return "the mail server refused the connection"
    if isinstance(exc, OSError):
        return "the mail server could not be reached"
    return "the message could not be sent"


def _deliver(
    smtp: SmtpSettings, message: EmailMessage, timeout: float, context: ssl.SSLContext
) -> None:
    """Send one message (blocking): TLS as configured, sign in if a username is set."""
    if smtp.tls not in ("starttls", "implicit") and not is_loopback(smtp.host):
        raise SmtpRefused(_PLAIN_REFUSED)
    # EHLO names the sender's domain: smtplib's default, socket.getfqdn(), names this
    # machine and is a reverse lookup that can stall.
    greeting = _domain(smtp.sender_address)
    client: smtplib.SMTP
    if smtp.tls == "implicit":
        client = smtplib.SMTP_SSL(smtp.host, smtp.port, greeting, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(smtp.host, smtp.port, greeting, timeout=timeout)
    with client:
        if smtp.tls == "starttls":
            client.starttls(context=context)
        if smtp.username:
            client.login(smtp.username, smtp.password)
        client.send_message(message, from_addr=smtp.sender_address, to_addrs=[str(message["To"])])


def _domain(address: str) -> str:
    return address.rpartition("@")[2] or "localhost"


def _envelope(
    smtp: SmtpSettings, to: str, subject: str, *, name: str = "", reply_to: str | None = None
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((smtp.sender_name or name, smtp.sender_address))
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(usegmt=True)
    # The sender's domain, never this machine's name.
    message["Message-ID"] = make_msgid(domain=_domain(smtp.sender_address))
    if reply_to:
        message["Reply-To"] = reply_to
    return message


def _email_of(message: MailMessage, smtp: SmtpSettings) -> EmailMessage:
    """warden's rendered message as a MIME message from the configured sender.

    The sender is ``mail.smtp``'s; warden's own ``mail.from_name`` names it
    only when ``mail.smtp.sender_name`` is empty.
    """
    email = _envelope(
        smtp,
        message.to,
        message.subject,
        name=message.from_name or "",
        reply_to=message.reply_to,
    )
    email.set_content(message.text)
    if message.html is not None:
        email.add_alternative(message.html, subtype="html")
    return email


class SmtpSender:
    """A ``MailSender`` over one :class:`SmtpSettings`; sends on a worker thread with a timeout.

    ``context`` replaces :func:`tls_context` (tests trusting their own authority).
    """

    def __init__(
        self,
        smtp: SmtpSettings,
        *,
        timeout: float = SEND_TIMEOUT,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self._smtp = smtp
        self._timeout = timeout
        self._context = context

    async def send(self, message: MailMessage) -> None:
        await self.deliver(_email_of(message, self._smtp), message.kind.value)

    async def deliver(self, email: EmailMessage, kind: str) -> None:
        """Send ``email``; :class:`SmtpRefused` with the reason if it was not delivered."""
        context = self._context or tls_context()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_deliver, self._smtp, email, self._timeout, context),
                timeout=self._timeout,
            )
        except SmtpRefused as exc:
            reason = exc.reason
        except Exception as exc:  # noqa: BLE001 - every failure becomes a fixed reason
            reason = _reason(exc)
        else:
            return
        logger.warning("Mail not delivered (%s message): %s", kind, reason)
        raise SmtpRefused(reason) from None


class InstanceMail:
    """warden's ``MailSender`` on a server: ``mail.smtp`` when a host is set, else ``fallback``.

    The settings and the password are read at each message, so a change by an
    administrator applies to the next one without a restart.
    """

    def __init__(self, secrets: SecretReader | None, fallback: MailSender) -> None:
        self._secrets = secrets
        self.fallback = fallback

    async def send(self, message: MailMessage) -> None:
        # The secrets store may be remote (AWS Secrets Manager): read it off the loop.
        smtp = await asyncio.to_thread(smtp_settings, self._secrets)
        if smtp is None:
            await self.fallback.send(message)
            return
        await SmtpSender(smtp).send(message)


# -- The administrator's test message -------------------------------------------------------

TEST_SUBJECT = "Spark test message"
_TEST_TEXT = (
    "This message was sent from Spark's instance settings to check its mail settings.\n\n"
    "If you received it, invitations and password reset links will be delivered.\n"
)


def build_test_mail(smtp: SmtpSettings, to: str) -> EmailMessage:
    """The test message to ``to`` from the configured sender."""
    email = _envelope(smtp, to, TEST_SUBJECT)
    email.set_content(_TEST_TEXT)
    return email


async def send_test_mail(
    secrets: SecretReader | None,
    to: str,
    *,
    timeout: float = SEND_TIMEOUT,
    context: ssl.SSLContext | None = None,
) -> None:
    """Send the test message to ``to`` with the current settings.

    Raises :class:`MailNotConfigured` without a host and :class:`SmtpRefused`
    when it was not delivered.
    """
    smtp = await asyncio.to_thread(smtp_settings, secrets)
    if smtp is None:
        raise MailNotConfigured("mail.smtp.host is not set")
    problem = settings_problem(
        {HOST: smtp.host, TLS: smtp.tls, SENDER_ADDRESS: smtp.sender_address}
    )
    if problem is not None:
        raise SmtpRefused(problem[1])
    sender = SmtpSender(smtp, timeout=timeout, context=context)
    await sender.deliver(build_test_mail(smtp, to), "test")
