"""Start-up configuration through konfig, validated before SPARK serves anything.

Read from konfig (``config.yaml``, ``SPARK__...`` environment variables, an AWS
settings document) and its secrets store:

* ``database.url``: a URL, or a ``secret://`` reference resolved through the store;
* ``server.public_base_url``: the address users reach SPARK on, required in
  server mode (https unless loopback, an origin with no path); warden's own
  public address is derived from it, so it is set once;
* ``warden.*``: warden's settings (identity, audit, syslog, mail, single
  sign-on, keys), validated by warden in server mode; its ``http`` section is
  SPARK's to derive (:func:`spark.identity.runtime.warden_config`);
* the secrets store itself: in server mode konfig's encrypted store with
  ``KONFIG_MASTER_KEY`` (one key shared by every replica) or AWS Secrets Manager,
  never the OS keychain.

Every refusal is a :class:`StartupConfigError` (a :class:`spark.mode.ModeConfigError`)
whose message names the setting and never contains a secret.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from spark import mode
from spark.instance_settings import registry
from spark.instance_settings.registry import SettingsError
from spark.instance_settings.secrets import SECRET_PREFIX, SecretsLike
from spark.mode import Mode, ModeConfigError, RuntimeSettings

if TYPE_CHECKING:
    from warden.config import WardenConfig

__all__ = [
    "StartupConfig",
    "StartupConfigError",
    "database_view",
    "public_base_url",
    "secrets_backend",
    "validate",
]

_SERVER_BACKENDS = ("encrypted_file", "aws_secrets_manager")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_UNSPECIFIED = frozenset({"0.0.0.0", "::"})


class StartupConfigError(ModeConfigError):
    """The start-up configuration is invalid; SPARK refuses to start."""


class KonfigLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


@dataclass(frozen=True)
class StartupConfig:
    runtime: RuntimeSettings
    secrets_backend: str
    public_base_url: str = ""
    # warden's validated configuration (server mode only).
    warden: WardenConfig | None = None
    warnings: tuple[str, ...] = ()


class _DatabaseView:
    """konfig's settings with ``database.url`` resolved through the secrets store."""

    def __init__(self, settings: KonfigLike, url: str | None) -> None:
        self._settings = settings
        self._url = url

    def get(self, key: str, default: Any = None) -> Any:
        if key == "database.url" and self._url is not None:
            return self._url
        return self._settings.get(key, default)


def _resolved_database_url(settings: KonfigLike, store: SecretsLike) -> str | None:
    value = settings.get("database.url", None)
    if not isinstance(value, str) or not value.startswith(SECRET_PREFIX):
        return None
    name = value[len(SECRET_PREFIX) :]
    resolved = store.get(name) if name else None
    if not resolved:
        raise StartupConfigError(
            f"database.url refers to the secret {name!r}, which is not set in the secrets "
            "store; set it with konfig (it is named 'database_url' since 0.3.0)"
        )
    return resolved


def database_view(settings: KonfigLike, store: SecretsLike) -> mode.SettingsLike:
    """The settings :func:`spark.mode.resolve` reads, with a ``secret://`` URL resolved."""
    return _DatabaseView(settings, _resolved_database_url(settings, store))


def secrets_backend(settings: KonfigLike, environ: Mapping[str, str], server: bool) -> str:
    """Which konfig secrets backend is in use; refuse one that server mode cannot share."""
    arn = environ.get("KONFIG_AWS_SECRETS_MANAGER", "")
    if arn:
        from konfig._aws import parse_region

        try:
            parse_region(arn)
        except ValueError:
            raise StartupConfigError(
                "KONFIG_AWS_SECRETS_MANAGER must be the ARN of a Secrets Manager secret"
            ) from None
        return "aws_bundle"
    configured = settings.get("secrets.backend", None)
    if not server:
        return str(configured) if configured else "keyring"
    if configured not in _SERVER_BACKENDS:
        raise StartupConfigError(
            "server mode keeps secrets in konfig's encrypted store or AWS Secrets Manager: "
            "set secrets.backend to encrypted_file (with KONFIG_MASTER_KEY) or "
            "aws_secrets_manager, or set KONFIG_AWS_SECRETS_MANAGER"
        )
    if configured == "encrypted_file":
        if not (environ.get("KONFIG_MASTER_KEY") or settings.get("secrets.master_key", None)):
            raise StartupConfigError(
                "konfig's encrypted store in server mode needs KONFIG_MASTER_KEY, the one "
                "master key every replica shares"
            )
    return str(configured)


def _checked(settings: KonfigLike, key: str) -> Any:
    raw = settings.get(key, None)
    if raw is None:
        return registry.spec(key).default
    try:
        return registry.coerce(key, raw)
    except SettingsError as exc:
        raise StartupConfigError(str(exc)) from None


def public_base_url(value: object) -> str:
    """The server's public address, as an origin without a trailing slash.

    Server mode needs it: sign-in cookies, the Origin pin and every link SPARK
    and warden send are built from it. It must be ``https://host[:port]`` (plain
    http only for a loopback host), with no path, query, fragment or
    credentials. Raises :class:`StartupConfigError` naming the setting.
    """
    text = str(value or "").strip()
    if not text:
        raise StartupConfigError(
            "server mode needs server.public_base_url, the address users reach Spark on "
            "(for example https://spark.example.com)"
        )
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise StartupConfigError("server.public_base_url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise StartupConfigError("server.public_base_url must be an absolute https URL")
    if parts.scheme == "http" and host not in _LOOPBACK:
        raise StartupConfigError(
            "server.public_base_url must use https unless the host is loopback"
        )
    if host in _UNSPECIFIED:
        raise StartupConfigError(
            "server.public_base_url must name the host users reach, not a bind address"
        )
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "?" in text
        or "#" in text
        or parts.username is not None
        or parts.password is not None
    ):
        raise StartupConfigError(
            "server.public_base_url must be scheme://host[:port] only: Spark is served at "
            "the root of its address, with no path, query or credentials"
        )
    return text.rstrip("/")


def validate(
    settings: KonfigLike,
    store: SecretsLike,
    *,
    default_sqlite_path: Path,
    environ: Mapping[str, str] | None = None,
) -> StartupConfig:
    """Validate the start-up configuration; return it, or raise :class:`StartupConfigError`."""
    env = os.environ if environ is None else environ
    runtime = mode.resolve(database_view(settings, store), default_sqlite_path=default_sqlite_path)
    server = runtime.mode is Mode.SERVER
    backend = secrets_backend(settings, env, server)
    base_url = str(_checked(settings, "server.public_base_url"))
    warden: WardenConfig | None = None
    if server:
        from spark.identity.runtime import warden_config

        base_url = public_base_url(base_url)
        warden = warden_config(settings, store, base_url)
    return StartupConfig(
        runtime=runtime,
        secrets_backend=backend,
        public_base_url=base_url,
        warden=warden,
    )
