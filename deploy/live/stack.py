"""Prepare, start and stop the live end-to-end stack (A18). Test only; never a deployment.

Run from the repository root with the project's virtual environment:

    python deploy/live/stack.py --sandbox DIR prepare --project NAME --port-base N --image TAG
    python deploy/live/stack.py --sandbox DIR up [--live]
    python deploy/live/stack.py --sandbox DIR down
    python deploy/live/stack.py --sandbox DIR secret NAME [--announce KEY]
    python deploy/live/stack.py --sandbox DIR compose ARGS...

``prepare`` writes everything the stack needs into ``DIR/live``: a throwaway CA
and the certificates it signs (OIDC provider, mail catcher), a CA bundle (the
public CAs plus the throwaway CA), the mock-oauth2-server configuration, the
env file with ``-dev-only`` credentials and a fresh konfig key, and
``live.json``, which the suite in ``tests/live`` reads (``SPARK_LIVE_STACK``).
It keeps what an earlier run wrote, so the konfig key never changes under a
running stack. Nothing it writes belongs in the repository.

``up`` migrates, stores the SSO client secret in the konfig store, starts every
service, waits until both web replicas report ready and creates the first
administrator with ``spark identity create-admin``. Without ``--live`` the web
replicas send Anthropic requests to the scripted model server; ``--live`` leaves
that out, for the one live chat. ``down`` removes the stack and its volumes.
``secret`` asks for one secret at this terminal (never echoed) and writes it into
the stack's konfig store with ``set_secret.py``; ``--announce KEY`` then tells the
running replicas, as the settings page would (for example ``secret
providers_anthropic_api_key --announce providers.anthropic.api_key``).

Ports are ``N`` plus a fixed offset (web 0, web2 1, OIDC 10, model stub 11,
mail API 25, PostgreSQL 32), all bound to 127.0.0.1.
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
MAILPIT_VERSION = "v1.21"
PYTHON_VERSION = "3.12-slim"

ADMIN_EMAIL = "admin@example.com"
SSO_CLIENT_ID = "spark"
SSO_EMAIL = "ada@example.com"
DB_PASSWORD = "spark-live-db-dev-only"  # noqa: S105 - fixed sandbox value
SSO_CLIENT_SECRET = "spark-live-sso-dev-only"  # noqa: S105 - fixed sandbox value
P12_PASSWORD = "spark-live-p12-dev-only"  # noqa: S105 - fixed sandbox value


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
    mail_key, mail_cert = leaf("mail", ["mail"], [])
    (certs / "mail.crt").write_bytes(mail_cert.public_bytes(pem) + ca_pem)
    (certs / "mail.key").write_bytes(_pem_key(mail_key))

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


def prepare(sandbox: Path, project: str, port_base: int, image: str) -> Path:
    live = sandbox / "live"
    certs = live / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    ports = {name: port_base + offset for name, offset in OFFSETS.items()}
    public = f"http://127.0.0.1:{ports['web']}"

    if not (certs / "ca.crt").exists():
        _certificates(certs)
    _oidc_config(certs)

    spark_env = live / "spark.env"
    if not spark_env.exists():
        from cryptography.fernet import Fernet

        _lines(
            spark_env,
            {
                "POSTGRES_USER": "spark",
                "POSTGRES_PASSWORD": DB_PASSWORD,
                "POSTGRES_DB": "spark",
                "SPARK__DATABASE__URL": f"postgresql://spark:{DB_PASSWORD}@postgres:5432/spark",
                "SPARK__SERVER__PUBLIC_BASE_URL": public,
                "KONFIG_MASTER_KEY": Fernet.generate_key().decode(),
                "SPARK__WARDEN__SSO__ENABLED": "true",
                "SPARK__WARDEN__SSO__ISSUER_URL": "https://oidc:8443/default",
                "SPARK__WARDEN__SSO__CLIENT_ID": SSO_CLIENT_ID,
                "SPARK__WARDEN__SSO__SCOPES": "openid,email",
            },
        )

    _lines(
        live / "live.env",
        {
            "SPARK_IMAGE": image,
            "SPARK_ENV_FILE": _posix(spark_env),
            "SPARK_WEB_PORT": str(ports["web"]),
            "LIVE_WEB2_PORT": str(ports["web2"]),
            "LIVE_OIDC_PORT": str(ports["oidc"]),
            "LIVE_STUB_PORT": str(ports["stub"]),
            "LIVE_MAIL_API_PORT": str(ports["mail_api"]),
            "LIVE_POSTGRES_PORT": str(ports["postgres"]),
            "LIVE_CERTS": _posix(certs),
            "LIVE_TOOLS": _posix(HERE),
            "LIVE_OIDC_VERSION": OIDC_VERSION,
            "LIVE_MAILPIT_VERSION": MAILPIT_VERSION,
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
            "compose": _compose_prefix(live, project, scripted=True),
            "compose_live": _compose_prefix(live, project, scripted=False),
        }
    )
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8", newline="\n")
    return state_path


# -- Docker Compose --------------------------------------------------------------------------


def _compose_prefix(live: Path, project: str, *, scripted: bool) -> list[str]:
    files = [DEPLOY / "compose.yaml", HERE / "compose.live.yaml"]
    if scripted:
        files.append(HERE / "compose.anthropic-stub.yaml")
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


def up(sandbox: Path, *, live_provider: bool) -> None:
    state = _state(sandbox)
    compose = state["compose_live" if live_provider else "compose"]
    _run(compose + ["up", "-d", "postgres", "oidc", "mail", "model-stub"])
    _run(compose + ["run", "--rm", "-T", "migrate"])
    _run(
        compose
        + ["run", "--rm", "-T", "--no-deps", "--entrypoint", "python", "migrate", "-I"]
        + ["/opt/spark-live/set_secret.py", "warden.sso.client-secret"],
        stdin=SSO_CLIENT_SECRET + "\n",
    )
    _run(compose + ["up", "-d"])
    _ready(state["base_url"])
    _ready(state["web2_url"])
    _run(
        compose
        + ["run", "--rm", "-T", "--no-deps", "web", "identity", "create-admin"]
        + ["--email", state["admin_email"]],
        stdin=state["admin_password"] + "\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sandbox", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--project", required=True)
    p.add_argument("--port-base", type=int, required=True)
    p.add_argument("--image", required=True)
    u = sub.add_parser("up")
    u.add_argument("--live", action="store_true", help="leave the Anthropic stub out")
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
        up(sandbox, live_provider=args.live)
    elif args.command == "secret":
        state = _state(sandbox)
        command = state["compose_live"] + ["run", "--rm", "-it", "--no-deps"]
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
