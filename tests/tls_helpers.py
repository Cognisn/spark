"""Test-only helpers: a PostgreSQL stand-in that speaks only the TLS opening.

``FakePostgresTLS`` listens on loopback, answers libpq's SSLRequest with ``S``,
and completes (or refuses) the TLS handshake with a certificate signed by a
test authority. It then closes the connection: what the client does up to that
point (find the root certificate, verify the chain and the host name) is what
the tests observe, with no PostgreSQL and no Docker. Certificates are generated
per test run and never leave the temporary directory.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SSL_REQUEST_CODE = 80877103


@dataclass(frozen=True)
class Authority:
    """A test certificate authority, with a server certificate for 127.0.0.1."""

    ca_pem: Path
    server_cert: Path
    server_key: Path


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def make_authority(directory: Path, name: str) -> Authority:
    """A CA named ``name`` and a server certificate (SAN 127.0.0.1) it signs."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(f"{name} test authority"))
        .issuer_name(_name(f"{name} test authority"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("db.example.com"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    ca_pem = directory / f"{name}-ca.pem"
    server_cert = directory / f"{name}-server.pem"
    server_key = directory / f"{name}-server.key"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_key(server_key, key)
    return Authority(ca_pem, server_cert, server_key)


class FakePostgresTLS:
    """Answers SSLRequest with ``S`` and handshakes with ``authority``'s server certificate.

    A client on a busy machine (cold imports, antivirus scanning, other test runs) can take
    many seconds between connecting and sending its ClientHello. The stand-in therefore gives
    each connection ``connection_timeout`` seconds per step, far longer than any healthy client
    needs, so a slow client is never miscounted as a failed handshake; it only ever waits on
    the client, never on a clock the tests race. Tests wait for the outcome with
    :meth:`wait_outcome`, which blocks on an event set after the counters are updated.
    """

    #: Seconds a connection may take per step (header, handshake). A hung client still ends.
    connection_timeout = 60.0

    def __init__(self, authority: Authority) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(authority.server_cert), str(authority.server_key))
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.2)
        self.port: int = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._outcome = threading.Event()
        self.ssl_requests = 0
        self.handshakes_ok = 0
        self.handshakes_failed = 0
        self.plaintext_startups = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakePostgresTLS:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(5)
        self._listener.close()

    def url(self, user: str = "spark_web", **query: str) -> str:
        """A verify-full URL with no sslrootcert (the AWS layout), unless ``query`` adds some."""
        parts = {"sslmode": "verify-full", **query}
        text = "&".join(f"{k}={v}" for k, v in parts.items())
        return f"postgresql://{user}:CHANGEME@127.0.0.1:{self.port}/spark?{text}"

    def wait_for(self, condition: str, count: int = 1, timeout: float = 15.0) -> bool:
        """Wait until ``handshakes_ok`` (or another counter) reaches ``count``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self, condition) >= count:
                return True
            time.sleep(0.05)
        return False

    def wait_outcome(self, timeout: float = 40.0) -> bool:
        """Block until a handshake has succeeded or failed (True), or ``timeout`` passes (False)."""
        return self._outcome.wait(timeout)

    def _bump(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)
            if name in ("handshakes_ok", "handshakes_failed"):
                self._outcome.set()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(self.connection_timeout)
        try:
            header = conn.recv(8)
            if len(header) == 8 and struct.unpack("!ii", header)[1] == SSL_REQUEST_CODE:
                self._bump("ssl_requests")
                conn.sendall(b"S")
                try:
                    tls = self._context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    self._bump("handshakes_failed")
                    return
                self._bump("handshakes_ok")
                tls.close()
            elif header:
                # A startup message with no TLS: the client did not insist on it.
                self._bump("plaintext_startups")
        except OSError:
            pass
        finally:
            conn.close()
