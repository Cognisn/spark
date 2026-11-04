"""Test-only: a small SMTP server on 127.0.0.1 for delivery tests, with no dependency.

It speaks as much ESMTP as ``smtplib`` needs: EHLO and HELO, STARTTLS, AUTH
PLAIN and LOGIN, MAIL, RCPT, DATA, RSET, NOOP and QUIT, in plain text, with
STARTTLS, or with implicit TLS. Each message it accepts is kept with what the
session looked like when it arrived (TLS or not, who signed in), and every
sign-in attempt is kept with whether it came over TLS.

Certificates come from :func:`make_certificates`: a test authority and a
server certificate for 127.0.0.1 and localhost, made with ``cryptography``
(already installed with warden). Every name and credential here is synthetic.
"""

from __future__ import annotations

import base64
import email
import email.policy
import ipaddress
import socket
import ssl
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

Security = Literal["none", "starttls", "implicit"]

USERNAME = "spark-mailer@example.com"
PASSWORD = "smtp-password-dev-only"


@dataclass(frozen=True)
class Certificates:
    """The test authority (to trust) and the server's certificate chain and key."""

    authority: Path
    chain: Path
    key: Path

    def client_context(self) -> ssl.SSLContext:
        """A verifying client context that trusts only the test authority."""
        context = ssl.create_default_context(cafile=str(self.authority))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.chain), str(self.key))
        return context


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def make_certificates(directory: Path) -> Certificates:
    """A test authority and a server certificate for 127.0.0.1 and localhost, in ``directory``."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("Spark test authority dev-only")
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("127.0.0.1"))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    authority = directory / "authority.pem"
    chain = directory / "server.pem"
    key_file = directory / "server.key"
    authority.write_bytes(ca.public_bytes(pem))
    chain.write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    key_file.write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return Certificates(authority, chain, key_file)


@dataclass
class Received:
    """One accepted message and the session it arrived in."""

    mail_from: str
    recipients: list[str]
    data: bytes
    tls: bool
    user: str | None

    def message(self) -> EmailMessage:
        parsed = email.message_from_bytes(self.data, policy=email.policy.default)
        assert isinstance(parsed, EmailMessage)
        return parsed


@dataclass
class SignIn:
    user: str
    tls: bool
    accepted: bool


@dataclass
class FakeSmtp:
    """A threaded SMTP server on 127.0.0.1 (a context manager; ``port`` once entered).

    ``security`` is how a client must talk to it; ``advertise_starttls=False``
    makes a STARTTLS server that never offers it. With ``credentials`` a
    message needs a successful sign-in first. ``silent`` accepts connections
    and never answers.
    """

    security: Security = "none"
    certificates: Certificates | None = None
    advertise_starttls: bool = True
    credentials: tuple[str, str] | None = None
    silent: bool = False
    received: list[Received] = field(default_factory=list)
    sign_ins: list[SignIn] = field(default_factory=list)
    greetings: list[str] = field(default_factory=list)
    connections: int = 0

    def __post_init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port: int = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        if self.security != "none" and self.certificates is None:
            raise ValueError("a TLS server needs certificates")

    def __enter__(self) -> FakeSmtp:
        accept = threading.Thread(target=self._accept, daemon=True)
        accept.start()
        self._threads.append(accept)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._listener.close()
        for thread in self._threads:
            thread.join(timeout=5)

    def last(self) -> EmailMessage:
        assert self.received, "no message was received"
        return self.received[-1].message()

    # -- serving ------------------------------------------------------------------

    def _accept(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self.connections += 1
            worker = threading.Thread(target=self._session, args=(conn,), daemon=True)
            worker.start()
            self._threads.append(worker)

    def _session(self, conn: socket.socket) -> None:
        try:
            _Session(self, conn).run()
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            conn.close()


class _Session:
    def __init__(self, server: FakeSmtp, conn: socket.socket) -> None:
        self.server = server
        self.sock: socket.socket = conn
        self.buffer = b""
        self.tls = False
        self.user: str | None = None
        self.mail_from: str | None = None
        self.recipients: list[str] = []

    def send(self, line: str) -> None:
        self.sock.sendall(line.encode("ascii") + b"\r\n")

    def readline(self) -> bytes:
        while b"\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\r\n")
        return line

    def upgrade(self) -> None:
        assert self.server.certificates is not None
        context = self.server.certificates.server_context()
        self.sock = context.wrap_socket(self.sock, server_side=True)
        self.tls = True
        self.buffer = b""

    def run(self) -> None:
        self.sock.settimeout(10)
        if self.server.silent:
            while not self.server._stop.is_set():
                try:
                    if not self.sock.recv(1024):
                        return
                except TimeoutError:
                    continue
            return
        if self.server.security == "implicit":
            self.upgrade()
        self.send("220 smtp.example.com ESMTP fake")
        while True:
            line = self.readline().decode("utf-8", "replace")
            verb, _, rest = line.partition(" ")
            handler = getattr(self, "do_" + verb.upper(), None)
            if handler is None:
                self.send("500 5.5.1 Unknown command")
            elif handler(rest) is False:
                return

    def _offers_starttls(self) -> bool:
        return (
            self.server.security == "starttls" and self.server.advertise_starttls and not self.tls
        )

    def do_EHLO(self, name: str) -> None:
        self.server.greetings.append(name)
        lines = ["smtp.example.com", "8BITMIME", "AUTH PLAIN LOGIN"]
        if self._offers_starttls():
            lines.append("STARTTLS")
        for line in lines[:-1]:
            self.send("250-" + line)
        self.send("250 " + lines[-1])

    def do_HELO(self, name: str) -> None:
        self.server.greetings.append(name)
        self.send("250 smtp.example.com")

    def do_STARTTLS(self, _: str) -> None:
        if not self._offers_starttls():
            self.send("502 5.5.1 STARTTLS not offered")
            return
        self.send("220 2.0.0 Ready to start TLS")
        self.upgrade()
        self.user = None

    def _check(self, user: str, password: str) -> None:
        accepted = self.server.credentials is None or (user, password) == self.server.credentials
        self.server.sign_ins.append(SignIn(user, self.tls, accepted))
        if accepted:
            self.user = user
            self.send("235 2.7.0 Authentication successful")
        else:
            self.send("535 5.7.8 Authentication credentials invalid")

    def do_AUTH(self, rest: str) -> None:
        mechanism, _, initial = rest.partition(" ")
        if mechanism.upper() == "PLAIN":
            if not initial:
                self.send("334 ")
                initial = self.readline().decode("ascii")
            _, user, password = base64.b64decode(initial).decode("utf-8").split("\0")
            self._check(user, password)
        elif mechanism.upper() == "LOGIN":
            self.send("334 VXNlcm5hbWU6")
            user = base64.b64decode(self.readline()).decode("utf-8")
            self.send("334 UGFzc3dvcmQ6")
            password = base64.b64decode(self.readline()).decode("utf-8")
            self._check(user, password)
        else:
            self.send("504 5.5.4 Unrecognised authentication type")

    def do_MAIL(self, rest: str) -> None:
        if self.server.credentials is not None and self.user is None:
            self.send("530 5.7.0 Authentication required")
            return
        self.mail_from = rest.partition(":")[2].split(" ")[0].strip("<>")
        self.recipients = []
        self.send("250 2.1.0 OK")

    def do_RCPT(self, rest: str) -> None:
        self.recipients.append(rest.partition(":")[2].split(" ")[0].strip("<>"))
        self.send("250 2.1.5 OK")

    def do_DATA(self, _: str) -> None:
        self.send("354 End data with <CR><LF>.<CR><LF>")
        lines: list[bytes] = []
        while True:
            line = self.readline()
            if line == b".":
                break
            lines.append(line[1:] if line.startswith(b"..") else line)
        self.server.received.append(
            Received(
                self.mail_from or "",
                list(self.recipients),
                b"\r\n".join(lines) + b"\r\n",
                self.tls,
                self.user,
            )
        )
        self.send("250 2.0.0 Queued")

    def do_RSET(self, _: str) -> None:
        self.mail_from, self.recipients = None, []
        self.send("250 2.0.0 OK")

    def do_NOOP(self, _: str) -> None:
        self.send("250 2.0.0 OK")

    def do_QUIT(self, _: str) -> bool:
        self.send("221 2.0.0 Bye")
        return False
