"""Prepare, start and stop the live end-to-end stack (A18). Test only; never a deployment.

Run from the repository root with the project's virtual environment:

    python deploy/live/stack.py --sandbox DIR prepare --project NAME --port-base N --image TAG
    python deploy/live/stack.py --sandbox DIR up
    python deploy/live/stack.py --sandbox DIR down
    python deploy/live/stack.py --sandbox DIR secret NAME [--announce KEY]
    python deploy/live/stack.py --sandbox DIR compose ARGS...

``prepare`` writes everything the stack needs into ``DIR/live``: a throwaway CA
and the certificates it signs (OIDC provider, mail catcher, the model stub's
https API), a CA bundle (the public CAs plus the throwaway CA), the
mock-oauth2-server configuration, the env file with ``-dev-only`` credentials, a
fresh konfig key, the bootstrap settings (a synthetic first administrator at
``example.com``) and the start-up mail settings, and ``live.json``, which the
suite in ``tests/live`` reads (``SPARK_LIVE_STACK``). It keeps what an earlier
run wrote, so the konfig key never changes under a running stack, and adds any
setting a newer stack needs. Nothing it writes belongs in the repository.

``up`` starts PostgreSQL, the mail catcher, the OIDC provider and the model stub,
stores the SSO client secret in the konfig store, then starts the rest: the
``migrate`` service runs ``spark migrate && spark bootstrap`` (which mails the
first administrator's invitation to the catcher), and every Spark service starts
only once it has succeeded. ``up`` then waits until both web replicas report
ready. The suite accepts the invitation from the catcher's inbox; ``spark
identity create-admin`` is not run (it is the recovery path). Model calls go
wherever a connection's settings send them: the suite's connections name the
model stub, and the one live chat uses a connection of its own, so the same
stack serves both. ``down`` removes the stack and its volumes. ``secret`` asks for one secret
at this terminal (never echoed) and writes it into the stack's konfig store with
``set_secret.py``; ``--announce KEY`` then tells the running replicas, as the
connections screen would (for example ``secret connections.live.api_key
--announce connections.live.api_key`` replaces the key of the connection
``live``).

Ports are ``N`` plus a fixed offset (web 0, web2 1, OIDC 10, model stub 11,
mail API 25, PostgreSQL 32), all bound to 127.0.0.1. ``deploy/compose.yaml``'s
``mailpit`` service publishes web's port and its own inbox, since web shares its
network namespace. The model stub's https API (11443) and the MCP server
(127.0.0.1:8765, in the same namespace as web) are not published.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent

OFFSETS = {"web": 0, "web2": 1, "oidc": 10, "stub": 11, "mail_api": 25, "postgres": 32}
OIDC_VERSION = "6.1.3"
PYTHON_VERSION = "3.12-slim"

ADMIN_EMAIL = "admin@example.com"
ADMIN_FIRST_NAME = "Live"
ADMIN_LAST_NAME = "Administrator"
MAIL_HOST = "mailpit"
MAIL_SENDER = "spark@example.com"
MAIL_USERNAME = "spark"
MAIL_PASSWORD = "spark-live-mail-dev-only"  # noqa: S105 - fixed sandbox value
SSO_CLIENT_ID = "spark"
SSO_EMAIL = "ada@example.com"
DB_PASSWORD = "spark-live-db-dev-only"  # noqa: S105 - fixed sandbox value
SSO_CLIENT_SECRET = "spark-live-sso-dev-only"  # noqa: S105 - fixed sandbox value
P12_PASSWORD = "spark-live-p12-dev-only"  # noqa: S105 - fixed sandbox value
#: konfig names of the secrets ``up`` stores before bootstrap runs, with their values.
SECRETS = {
    "warden.sso.client-secret": SSO_CLIENT_SECRET,
    "mail_smtp_password": MAIL_PASSWORD,  # the mail.smtp.password instance setting
}


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


# -- Certificates -------------------------------------------------------------------------


def _key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_key(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _certificates(certs: Path) -> None:
    """The throwaway CA and the OIDC and mail certificates it signs."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SPARK live e2e throwaway CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
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

    def leaf(name: str, dns: list[str], ips: list[str]) -> tuple[Any, Any]:
        key = _key()
        sans: list[x509.GeneralName] = [x509.DNSName(d) for d in dns]
        sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
            .sign(ca_key, hashes.SHA256())
        )
        return key, cert

    pem = serialization.Encoding.PEM
    ca_pem = ca.public_bytes(pem)
    (certs / "ca.crt").write_bytes(ca_pem)

    oidc_key, oidc_cert = leaf("oidc", ["oidc", "localhost"], ["127.0.0.1"])
    (certs / "oidc.p12").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"oidc",
            oidc_key,
            oidc_cert,
            [ca],
            serialization.BestAvailableEncryption(P12_PASSWORD.encode()),
        )
    )
    # deploy/compose.yaml's catcher, reached by the name "mailpit" from every container
    # (web and migrate share its network namespace; web2 and the workers do not).
    mail_key, mail_cert = leaf(MAIL_HOST, [MAIL_HOST, "localhost"], ["127.0.0.1"])
    (certs / "mailpit.crt").write_bytes(mail_cert.public_bytes(pem) + ca_pem)
    (certs / "mailpit.key").write_bytes(_pem_key(mail_key))
    stub_key, stub_cert = leaf("model-stub", ["model-stub"], [])
    (certs / "model-stub.crt").write_bytes(stub_cert.public_bytes(pem) + ca_pem)
    (certs / "model-stub.key").write_bytes(_pem_key(stub_key))

    import certifi

    public = Path(certifi.where()).read_bytes()
    (certs / "ca-bundle.pem").write_bytes(public.rstrip() + b"\n" + ca_pem)


def _oidc_config(certs: Path) -> None:
    claims = {"sub": "ada", "email": SSO_EMAIL, "email_verified": True, "aud": [SSO_CLIENT_ID]}
    config = {
        "interactiveLogin": False,
        "httpServer": {
            "type": "NettyWrapper",
            "ssl": {
                "keyPassword": P12_PASSWORD,
                "keystoreFile": "/etc/spark-live/oidc.p12",
                "keystoreType": "PKCS12",
                "keystorePassword": P12_PASSWORD,
            },
        },
        "tokenCallbacks": [
            {
                "issuerId": "default",
                # The code exchange carries grant_type (not scope), so map on that.
                "requestMappings": [{"requestParam": "grant_type", "match": "*", "claims": claims}],
            }
        ],
    }
    (certs / "oidc.json").write_text(json.dumps(config, indent=2), encoding="utf-8", newline="\n")


# -- Files ----------------------------------------------------------------------------------


def _lines(path: Path, values: dict[str, str]) -> None:
    text = "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_lines(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            found[key.strip()] = value
    return found


def _spark_env(path: Path, public: str) -> None:
    """The Spark services' env file, kept as an earlier run wrote it (above all its konfig
    key), with any setting this stack needs that it lacks added."""
    from cryptography.fernet import Fernet

    existing = _read_lines(path) if path.exists() else {}
    wanted = {
        "POSTGRES_USER": "spark",
        "POSTGRES_PASSWORD": DB_PASSWORD,
        "POSTGRES_DB": "spark",
        "SPARK__DATABASE__URL": f"postgresql://spark:{DB_PASSWORD}@postgres:5432/spark",
        "SPARK__SERVER__PUBLIC_BASE_URL": public,
        "KONFIG_MASTER_KEY": existing.get("KONFIG_MASTER_KEY") or Fernet.generate_key().decode(),
        "SPARK__WARDEN__SSO__ENABLED": "true",
        "SPARK__WARDEN__SSO__ISSUER_URL": "https://oidc:8443/default",
        "SPARK__WARDEN__SSO__CLIENT_ID": SSO_CLIENT_ID,
        "SPARK__WARDEN__SSO__SCOPES": "openid,email",
        # spark bootstrap (the migrate service): the first administrator, invited by
        # mail, and the mail settings it stores while the database holds none.
        "SPARK__BOOTSTRAP__ADMIN__EMAIL": ADMIN_EMAIL,
        "SPARK__BOOTSTRAP__ADMIN__FIRST_NAME": ADMIN_FIRST_NAME,
        "SPARK__BOOTSTRAP__ADMIN__LAST_NAME": ADMIN_LAST_NAME,
        # Optional on AWS, left off here (no Bedrock in the sandbox), see docs/deployment/aws.md:
        # "SPARK__BOOTSTRAP__BEDROCK__REGION": "ap-southeast-2",
        # "SPARK__BOOTSTRAP__BEDROCK__NAME": "Amazon Bedrock (task role)",
        # "SPARK__BOOTSTRAP__BEDROCK__RESIDENCY_REGIONS": "ap-southeast-2,ap-southeast-4",
        # STARTTLS from the throwaway CA (SSL_CERT_FILE), so every container may send,
        # whether or not it shares the catcher's network namespace.
        "SPARK__MAIL__SMTP__HOST": MAIL_HOST,
        "SPARK__MAIL__SMTP__PORT": "1025",
        "SPARK__MAIL__SMTP__TLS": "starttls",
        "SPARK__MAIL__SMTP__SENDER_ADDRESS": MAIL_SENDER,
        "SPARK__MAIL__SMTP__SENDER_NAME": "SPARK live e2e",
        # The catcher accepts any sign-in; its password is the secret up stores.
        "SPARK__MAIL__SMTP__USERNAME": MAIL_USERNAME,
    }
    merged = {**wanted, **existing}
    if merged != existing:
        _lines(path, merged)


def prepare(sandbox: Path, project: str, port_base: int, image: str) -> Path:
    live = sandbox / "live"
    certs = live / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    ports = {name: port_base + offset for name, offset in OFFSETS.items()}
    public = f"http://127.0.0.1:{ports['web']}"

    if not all((certs / n).exists() for n in ("ca.crt", "model-stub.crt", "mailpit.crt")):
        _certificates(certs)
    _oidc_config(certs)

    spark_env = live / "spark.env"
    _spark_env(spark_env, public)

    _lines(
        live / "live.env",
        {
            "SPARK_IMAGE": image,
            "SPARK_ENV_FILE": _posix(spark_env),
            # deploy/compose.yaml's mailpit publishes web's port and its inbox.
            "SPARK_BIND": "127.0.0.1",
            "SPARK_WEB_PORT": str(ports["web"]),
            "SPARK_MAIL_PORT": str(ports["mail_api"]),
            "LIVE_WEB2_PORT": str(ports["web2"]),
            "LIVE_OIDC_PORT": str(ports["oidc"]),
            "LIVE_STUB_PORT": str(ports["stub"]),
            "LIVE_POSTGRES_PORT": str(ports["postgres"]),
            "LIVE_CERTS": _posix(certs),
            "LIVE_TOOLS": _posix(HERE),
            "LIVE_OIDC_VERSION": OIDC_VERSION,
            "LIVE_PYTHON_VERSION": PYTHON_VERSION,
        },
    )

    state_path = live / "live.json"
    state: dict[str, Any] = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
    state.setdefault("admin_password", "live-admin-" + secrets.token_urlsafe(18) + "-dev-only")
    state.update(
        {
            "project": project,
            "image": image,
            "sandbox": str(sandbox),
            "ports": ports,
            "base_url": public,
            "web2_url": f"http://127.0.0.1:{ports['web2']}",
            "oidc_url": f"https://127.0.0.1:{ports['oidc']}",
            "oidc_internal": "oidc:8443",
            "stub_url": f"http://127.0.0.1:{ports['stub']}",
            "mail_api": f"http://127.0.0.1:{ports['mail_api']}",
            "postgres_url": (
                f"postgresql://spark:{DB_PASSWORD}@127.0.0.1:{ports['postgres']}/spark"
            ),
            "ca_bundle": str(certs / "ca-bundle.pem"),
            "admin_email": ADMIN_EMAIL,
            "sso_email": SSO_EMAIL,
            "stub_internal": "http://model-stub:11434",
            "stub_tls_internal": "https://model-stub:11443",
            "mcp_url": "http://127.0.0.1:8765/mcp",
            "compose": _compose_prefix(live, project),
        }
    )
    state.pop("compose_live", None)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8", newline="\n")
    return state_path


# -- Docker Compose --------------------------------------------------------------------------


def _compose_prefix(live: Path, project: str) -> list[str]:
    files = [DEPLOY / "compose.yaml", HERE / "compose.live.yaml"]
    command = ["docker", "compose", "-p", project, "--env-file", str(live / "live.env")]
    for f in files:
        command += ["-f", str(f)]
    return command


def _state(sandbox: Path) -> dict[str, Any]:
    path = sandbox / "live" / "live.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run prepare first")
    state: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return state


def _run(command: list[str], *, stdin: str | None = None) -> None:
    print("+", " ".join(command[command.index("compose") + 1 :][-6:]), flush=True)
    subprocess.run(command, input=stdin, text=True, check=True)  # noqa: S603 - fixed argv


def _ready(url: str, timeout: float = 900.0) -> None:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                url + "/loading/api/status", timeout=5
            ) as resp:  # noqa: S310
                last = json.loads(resp.read())
                if last.get("ready"):
                    print(f"{url} ready", flush=True)
                    return
        except OSError as exc:
            last = str(exc)
        time.sleep(3)
    raise SystemExit(f"{url} not ready after {timeout:.0f}s: {last}")


def up(sandbox: Path) -> None:
    state = _state(sandbox)
    compose = state["compose"]
    # migrate shares the catcher's network namespace, so the catcher starts first.
    _run(compose + ["up", "-d", "postgres", "mailpit", "oidc", "model-stub"])
    for name, value in SECRETS.items():
        _run(
            compose
            + ["run", "--rm", "-T", "--no-deps", "--entrypoint", "python", "migrate", "-I"]
            + ["/opt/spark-live/set_secret.py", name],
            stdin=value + "\n",
        )
    # Compose runs migrate (spark migrate && spark bootstrap) and starts web, web2 and
    # the workers only once it has succeeded, so a failed bootstrap fails this step.
    try:
        _run(compose + ["up", "-d"])
    except subprocess.CalledProcessError:
        logs = compose + ["logs", "--no-color", "migrate"]
        subprocess.run(logs, check=False)  # noqa: S603 - fixed argv
        raise SystemExit("the migrate service (spark migrate && spark bootstrap) failed")
    _ready(state["base_url"])
    _ready(state["web2_url"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sandbox", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--project", required=True)
    p.add_argument("--port-base", type=int, required=True)
    p.add_argument("--image", required=True)
    sub.add_parser("up")
    sub.add_parser("down")
    s = sub.add_parser("secret")
    s.add_argument("name")
    s.add_argument("--announce", metavar="KEY")
    c = sub.add_parser("compose")
    c.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    sandbox = args.sandbox.resolve()
    if args.command == "prepare":
        print(prepare(sandbox, args.project, args.port_base, args.image))
    elif args.command == "up":
        up(sandbox)
    elif args.command == "secret":
        state = _state(sandbox)
        command = state["compose"] + ["run", "--rm", "-it", "--no-deps"]
        command += ["--entrypoint", "python", "web", "-I", "/opt/spark-live/set_secret.py"]
        command += [args.name] + (["--announce", args.announce] if args.announce else [])
        return subprocess.run(command, check=False).returncode  # noqa: S603 - fixed argv
    elif args.command == "down":
        _run(_state(sandbox)["compose"] + ["down", "-v"])
    else:
        subprocess.run(_state(sandbox)["compose"] + args.rest, check=False)  # noqa: S603
    return 0


if __name__ == "__main__":
    sys.exit(main())
