"""The bootstrap settings, read from konfig and checked all at once.

``spark bootstrap`` reads these (``config.yaml``, ``SPARK__`` variables, the AWS
settings document, at the same dotted paths) and refuses to run unless every one
is complete and valid. A refusal is one :class:`BootstrapSettingsError` listing
every problem by its dotted and ``SPARK__`` names, never a value:

* ``bootstrap.admin.email``, ``first_name`` and ``last_name``: the first
  administrator, invited through warden (warden keeps one display name, "First Last");
* ``bootstrap.database.web_url`` and ``worker_url``, both or neither: the URLs web
  and worker connect with, normally ``secret://`` references; their logins and
  passwords are created from them. Neither keeps the one-owner arrangement;
* ``worker.audit_purge_database_url``: the purge's own login, needed with restricted
  logins and an audit retention (``warden.audit.retention_days``);
* ``mail.smtp.*``: instance settings that bootstrap fills in from the start-up
  values while the database holds none (:func:`mail_to_seed`), so the invitation
  can be sent; a host and a sender address are required from one or the other;
* ``bootstrap.wait_seconds`` and ``bootstrap.wait_interval_seconds``: how long web
  and worker wait for bootstrap (:mod:`spark.bootstrap.wait`).

A URL that asks for a verified TLS connection (``sslmode=verify-ca`` or
``verify-full``) must name a readable root certificate (``sslrootcert`` or
``PGSSLROOTCERT``), checked by :func:`tls_problem`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from spark.db.roles import Login, login_of
from spark.identity import mail
from spark.instance_settings import registry
from spark.instance_settings.registry import SettingsError
from spark.instance_settings.startup import StartupConfigError

__all__ = [
    "ADMIN_EMAIL",
    "ADMIN_FIRST_NAME",
    "ADMIN_LAST_NAME",
    "DEFAULT_WAIT_INTERVAL_SECONDS",
    "DEFAULT_WAIT_SECONDS",
    "MAIL_KEYS",
    "PURGE_URL",
    "WAIT_INTERVAL_SECONDS",
    "WAIT_SECONDS",
    "WEB_URL",
    "WORKER_URL",
    "Administrator",
    "BootstrapSettings",
    "BootstrapSettingsError",
    "Logins",
    "collect",
    "configured",
    "env_name",
    "mail_to_seed",
    "named",
    "smtp_of",
    "tls_problem",
    "wait_limits",
]

ADMIN_EMAIL: Final = "bootstrap.admin.email"
ADMIN_FIRST_NAME: Final = "bootstrap.admin.first_name"
ADMIN_LAST_NAME: Final = "bootstrap.admin.last_name"
WEB_URL: Final = "bootstrap.database.web_url"
WORKER_URL: Final = "bootstrap.database.worker_url"
PURGE_URL: Final = "worker.audit_purge_database_url"
WAIT_SECONDS: Final = "bootstrap.wait_seconds"
WAIT_INTERVAL_SECONDS: Final = "bootstrap.wait_interval_seconds"
DEFAULT_WAIT_SECONDS: Final = 600.0
DEFAULT_WAIT_INTERVAL_SECONDS: Final = 5.0

DATABASE_URL: Final = "database.url"
RETENTION: Final = "warden.audit.retention_days"
#: The ``mail.smtp`` instance settings bootstrap may fill in (the password is a secret).
MAIL_KEYS: Final = (
    mail.HOST,
    mail.PORT,
    mail.TLS,
    mail.SENDER_ADDRESS,
    mail.SENDER_NAME,
    mail.USERNAME,
)
_SECRET_PREFIX: Final = "secret://"
_POSTGRES: Final = frozenset({"postgresql", "postgres"})
_VERIFIED: Final = frozenset({"verify-ca", "verify-full"})
_NAME_MAX: Final = 100


class KonfigLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


class SecretReader(Protocol):
    def get(self, key: str) -> str | None: ...


class BootstrapSettingsError(StartupConfigError):
    """The bootstrap settings are incomplete or invalid; ``problems`` names each one."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        listed = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"the bootstrap settings are not complete and valid:\n{listed}")


def env_name(key: str) -> str:
    """The ``SPARK__`` environment variable for a dotted setting."""
    return "SPARK__" + key.upper().replace(".", "__")


def named(key: str) -> str:
    """A setting as problems name it: ``dotted.key (SPARK__DOTTED__KEY)``."""
    return f"{key} ({env_name(key)})"


@dataclass(frozen=True, slots=True)
class Administrator:
    email: str
    first_name: str
    last_name: str

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True, slots=True)
class Logins:
    """The restricted logins, each from the URL it connects with."""

    web: Login
    worker: Login
    purger: Login | None = None

    def names(self) -> tuple[str, ...]:
        found = (self.web, self.worker, self.purger)
        return tuple(login.name for login in found if login is not None)


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    administrator: Administrator
    logins: Logins | None
    #: The ``mail.smtp`` values in force once bootstrap has run (stored, or seeded).
    mail: Mapping[str, Any] = field(default_factory=dict)
    #: The values bootstrap writes to the instance settings (empty when some are stored).
    mail_seed: Mapping[str, Any] = field(default_factory=dict)


def _text(settings: KonfigLike, key: str) -> str:
    value = settings.get(key, None)
    return "" if value is None else str(value).strip()


def configured(settings: KonfigLike) -> bool:
    """Whether this installation names a first administrator (it runs ``spark bootstrap``)."""
    return bool(_text(settings, ADMIN_EMAIL))


def wait_limits(settings: KonfigLike) -> tuple[float, float]:
    """``(total, interval)`` seconds web and worker wait for bootstrap; defaults on bad values."""

    def number(key: str, default: float) -> float:
        try:
            value = float(settings.get(key, default) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return (
        number(WAIT_SECONDS, DEFAULT_WAIT_SECONDS),
        number(WAIT_INTERVAL_SECONDS, DEFAULT_WAIT_INTERVAL_SECONDS),
    )


def _administrator(settings: KonfigLike, problems: list[str]) -> Administrator | None:
    from warden.identity import UserError
    from warden.identity.users import check_email

    email = _text(settings, ADMIN_EMAIL)
    first = _text(settings, ADMIN_FIRST_NAME)
    last = _text(settings, ADMIN_LAST_NAME)
    start = len(problems)
    if not email:
        problems.append(f"{named(ADMIN_EMAIL)} is not set: the first administrator's address")
    else:
        try:
            check_email(email)
        except UserError:
            problems.append(f"{named(ADMIN_EMAIL)} is not a valid email address")
    for key, value in ((ADMIN_FIRST_NAME, first), (ADMIN_LAST_NAME, last)):
        if not value:
            problems.append(f"{named(key)} is not set")
        elif len(value) > _NAME_MAX or any(ch in value for ch in "\r\n\x00"):
            problems.append(f"{named(key)} must be one line of at most {_NAME_MAX} characters")
    if len(problems) > start:
        return None
    return Administrator(email, first, last)


def _resolved(
    settings: KonfigLike, secrets: SecretReader, key: str, problems: list[str]
) -> str | None:
    """The URL a setting holds, a ``secret://`` reference resolved; None (with a problem) if not."""
    raw = settings.get(key, None)
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip()
    if value.startswith(_SECRET_PREFIX):
        name = value[len(_SECRET_PREFIX) :]
        resolved = secrets.get(name) if name else None
        if not resolved:
            problems.append(
                f"{named(key)} refers to the secret {name!r}, which is not set in the secrets store"
            )
            return None
        value = str(resolved).strip()
    scheme = urlsplit(value).scheme.lower() if "://" in value else ""
    if scheme == "postgresql+psycopg":
        value = "postgresql" + value[len(scheme) :]
    elif scheme not in _POSTGRES:
        problems.append(f"{named(key)} must be a PostgreSQL URL")
        return None
    return value


def _target(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port or 5432
    except ValueError:
        port = -1
    return host, port, unquote(parts.path.lstrip("/"))


def _login(key: str, url: str, owner_url: str, problems: list[str]) -> Login | None:
    try:
        login = login_of(url)
    except ValueError as exc:
        problems.append(f"{named(key)}: {exc}")
        return None
    if _target(url) != _target(owner_url):
        problems.append(
            f"{named(key)} must name the same host, port and database as {named(DATABASE_URL)}"
        )
        return None
    owner = unquote(urlsplit(owner_url).username or "")
    if login.name == owner:
        problems.append(f"{named(key)} must name its own login, not the schema owner's")
        return None
    return login


def tls_problem(key: str, url: str, environ: Mapping[str, str]) -> str | None:
    """Why a URL's TLS settings cannot work, naming ``key`` and never the URL; None when fine."""
    query = parse_qs(urlsplit(url).query)
    sslmode = (query.get("sslmode") or [environ.get("PGSSLMODE", "")])[0].lower()
    if sslmode not in _VERIFIED:
        return None
    root = (query.get("sslrootcert") or [environ.get("PGSSLROOTCERT", "")])[0]
    if root == "system":
        return None
    if not root:
        home = Path(environ.get("HOME") or os.path.expanduser("~"))
        root = str(home / ".postgresql" / "root.crt")
    path = Path(root)
    if not path.is_file() or not os.access(path, os.R_OK):
        return (
            f"{named(key)} asks for sslmode={sslmode}, but no readable root certificate was "
            "found (set sslrootcert in the URL or PGSSLROOTCERT)"
        )
    return None


def _logins(
    settings: KonfigLike,
    secrets: SecretReader,
    owner_url: str,
    environ: Mapping[str, str],
    problems: list[str],
) -> Logins | None:
    configured_keys = [k for k in (WEB_URL, WORKER_URL) if str(settings.get(k, None) or "").strip()]
    if not configured_keys:
        return None
    if len(configured_keys) == 1:
        missing = WORKER_URL if configured_keys == [WEB_URL] else WEB_URL
        problems.append(
            f"{named(missing)} is not set: restricted logins need both "
            f"{WEB_URL} and {WORKER_URL}, or neither"
        )
    found: dict[str, Login] = {}
    for key in (WEB_URL, WORKER_URL, PURGE_URL):
        url = _resolved(settings, secrets, key, problems)
        if url is None:
            continue
        tls = tls_problem(key, url, environ)
        if tls:
            problems.append(tls)
        login = _login(key, url, owner_url, problems)
        if login is not None:
            found[key] = login
    names = [login.name for login in found.values()]
    if len(set(names)) != len(names):
        problems.append(
            f"{named(WEB_URL)}, {named(WORKER_URL)} and {named(PURGE_URL)} must each name a "
            "different login"
        )
    retention = settings.get(RETENTION, None)
    if retention not in (None, "", 0, "0") and PURGE_URL not in found:
        if not str(settings.get(PURGE_URL, None) or "").strip():
            problems.append(
                f"{named(PURGE_URL)} is not set: with restricted logins and an audit retention "
                f"({RETENTION}) the purge needs its own login"
            )
    if WEB_URL not in found or WORKER_URL not in found:
        return None
    return Logins(found[WEB_URL], found[WORKER_URL], found.get(PURGE_URL))


def _startup_mail(settings: KonfigLike, problems: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in MAIL_KEYS:
        raw = settings.get(key, None)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            values[key] = registry.coerce(key, raw)
        except SettingsError:
            problems.append(f"{named(key)} is not valid")
    return values


def mail_to_seed(stored: Mapping[str, Any], startup: Mapping[str, Any]) -> dict[str, Any]:
    """The start-up ``mail.smtp`` values to store: all of them while no host is stored, else none."""
    if str(stored.get(mail.HOST) or "").strip():
        return {}
    if not str(startup.get(mail.HOST) or "").strip():
        return {}
    return dict(startup)


def _effective_mail(stored: Mapping[str, Any], seed: Mapping[str, Any]) -> dict[str, Any]:
    source = seed if seed else stored
    return {key: source.get(key, registry.spec(key).default) for key in MAIL_KEYS}


def _mail_problems(values: Mapping[str, Any], secrets: SecretReader, problems: list[str]) -> None:
    if not str(values.get(mail.HOST) or "").strip():
        problems.append(
            f"{named(mail.HOST)} and {named(mail.SENDER_ADDRESS)} are not set (nor stored on the "
            "instance settings page): the first administrator's invitation is sent by mail"
        )
        return
    found = mail.settings_problem(values)
    if found is not None:
        key, reason = found
        problems.append(f"{named(key)}: {reason}")
    if str(values.get(mail.USERNAME) or "").strip():
        secret = registry.secret_name(mail.PASSWORD)
        if not secrets.get(secret):
            problems.append(
                f"{mail.PASSWORD} (the secret {secret!r}) is not set, but {named(mail.USERNAME)} is"
            )


def smtp_of(values: Mapping[str, Any], secrets: SecretReader) -> mail.SmtpSettings:
    """The SMTP settings for the invitation, from the ``mail.smtp`` values in force."""
    return mail.SmtpSettings(
        host=str(values.get(mail.HOST) or "").strip(),
        port=int(values.get(mail.PORT) or 587),
        tls=str(values.get(mail.TLS) or "starttls"),
        sender_address=str(values.get(mail.SENDER_ADDRESS) or "").strip(),
        sender_name=str(values.get(mail.SENDER_NAME) or "").strip(),
        username=str(values.get(mail.USERNAME) or "").strip(),
        password=secrets.get(registry.secret_name(mail.PASSWORD)) or "",
    )


def collect(
    settings: KonfigLike,
    secrets: SecretReader,
    *,
    owner_url: str,
    stored_mail: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> BootstrapSettings:
    """Read and check every bootstrap setting; :class:`BootstrapSettingsError` lists what is wrong.

    ``owner_url`` is the resolved ``database.url`` (the schema owner's);
    ``stored_mail`` the ``mail.smtp`` instance settings already in the database.
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []
    owner_tls = tls_problem(DATABASE_URL, owner_url, env)
    if owner_tls:
        problems.append(owner_tls)
    administrator = _administrator(settings, problems)
    logins = _logins(settings, secrets, owner_url, env, problems)
    startup_mail = _startup_mail(settings, problems)
    seed = mail_to_seed(stored_mail, startup_mail)
    effective = _effective_mail(stored_mail, seed)
    _mail_problems(effective, secrets, problems)
    if problems or administrator is None:
        raise BootstrapSettingsError(problems)
    return BootstrapSettings(administrator, logins, effective, seed)
