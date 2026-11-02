"""warden's runtime in a SPARK server: configuration, keys, audit recorder and Identity.

SPARK embeds warden; nothing runs as a separate service. At web start-up, once
the database is open, :meth:`IdentityRuntime.start` builds:

* warden's configuration (:func:`warden_config`), derived from SPARK's
  settings: warden's public address is ``server.public_base_url`` (one
  setting, Q17), its routers' prefixes sit under :data:`IDENTITY_PREFIX`, and
  its mailed links point at SPARK's own screens; everything else comes from the
  ``warden`` section of konfig, with warden's defaults;
* warden's database handle over SPARK's psycopg pool;
* the audit recorder, installed process-wide before ``Identity`` (which
  registers its actions on it), carrying SPARK's own action catalogue
  (:mod:`spark.identity.audit`);
* the key store over konfig's secrets store (A9: secrets only through konfig).
  A server with no usable signing key rotates one in; a missing TOTP key and a
  missing CSRF key are generated into the secrets store. Replicas starting
  together take a PostgreSQL advisory lock for this, so they agree;
* ``Identity`` with SPARK's permissions and roles
  (:func:`spark.identity.permissions.build_registry`).

The web lifespan runs :meth:`IdentityRuntime.running` for the app's lifetime
and leaves it before the database closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets as _secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from warden.audit import ActionRegistry, AuditRecorder, set_recorder
from warden.config import ConfigError, WardenConfig, parse_config
from warden.identity import Identity, new_totp_key
from warden.keys import KeyStore, KeyStoreError

from spark.db.engine import Database
from spark.identity.audit import register_actions
from spark.identity.db import warden_database
from spark.identity.permissions import build_registry
from spark.instance_settings.startup import StartupConfigError, public_base_url

__all__ = [
    "ACCEPT_INVITATION_LINK",
    "CSRF_KEY_SECRET",
    "IDENTITY_PREFIX",
    "RESET_PASSWORD_LINK",
    "SSO_COMPLETION_LINK",
    "IdentityRuntime",
    "SecretsLike",
    "SettingsLike",
    "warden_config",
]

logger = logging.getLogger(__name__)

# Where warden's own routers would be mounted (step 5 decides which): never the
# browser's sign-in paths, which are SPARK's (spark.identity.web).
IDENTITY_PREFIX = "/identity"
_PREFIXES = {
    "auth_prefix": f"{IDENTITY_PREFIX}/auth",
    "admin_prefix": f"{IDENTITY_PREFIX}/admin",
    "audit_prefix": f"{IDENTITY_PREFIX}/audit",
    "keys_prefix": f"{IDENTITY_PREFIX}/keys",
}
# SPARK's screens that warden's mails and single sign-on link to.
ACCEPT_INVITATION_LINK = "/auth/invitation?token={token}"
RESET_PASSWORD_LINK = "/auth/reset?token={token}"
SSO_COMPLETION_LINK = "/auth/sso/complete"

# konfig secret holding the key that binds CSRF tokens to warden sessions.
CSRF_KEY_SECRET = "spark.csrf-key"  # noqa: S105 - a secret's name, not a secret

# Settings of the 0.2 placeholder for an external warden, refused by name.
_RETIRED = ("url", "client_id", "client_secret")
# pg_advisory_lock key for start-up bootstrap ("SPKW").
_BOOTSTRAP_LOCK = 0x53504B57


class SettingsLike(Protocol):
    """konfig's settings, as far as the runtime reads them."""

    def get(self, key: str, default: Any = None) -> Any: ...


class SecretsLike(Protocol):
    """konfig's secrets store: the key store and the TOTP key need ``set``."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


def _section(settings: SettingsLike) -> dict[str, Any]:
    get_section = getattr(settings, "get_section", None)
    found = get_section("warden") if callable(get_section) else settings.get("warden", None)
    return dict(found) if isinstance(found, dict) else {}


def warden_config(settings: SettingsLike, secrets: SecretsLike, base_url: str) -> WardenConfig:
    """warden's configuration for this server, validated; :class:`StartupConfigError` if not.

    ``base_url`` is the checked ``server.public_base_url``.
    """
    section = _section(settings)
    for key in _RETIRED:
        if key in section:
            raise StartupConfigError(
                f"warden.{key} is no longer used: warden runs inside Spark, so remove it "
                "(warden's settings are its identity, audit, syslog, mail, sso and keys sections)"
            )
    if "http" in section:
        raise StartupConfigError(
            "warden.http is derived from server.public_base_url and Spark's own paths; "
            "remove warden.http and set server.public_base_url instead"
        )
    data = {
        **section,
        "http": {
            "public_base_url": base_url,
            **_PREFIXES,
            "accept_invitation_link": ACCEPT_INVITATION_LINK,
            "reset_password_link": RESET_PASSWORD_LINK,
            "sso_completion_link": SSO_COMPLETION_LINK,
        },
    }
    try:
        return parse_config(data, secrets)
    except ConfigError as exc:
        raise StartupConfigError(str(exc)) from None


def _ensure_secret(secrets: SecretsLike, name: str, make: Callable[[], str]) -> str:
    """The secret ``name``, generated and stored first if it is missing."""
    value = secrets.get(name)
    if not value:
        logger.info("Generating %s in the secrets store", name)
        secrets.set(name, make())
        value = secrets.get(name)
    if not value:
        raise StartupConfigError(f"the secrets store did not keep {name}; check secrets.backend")
    return value


@dataclass(frozen=True, slots=True)
class IdentityRuntime:
    """warden inside one SPARK web process."""

    config: WardenConfig
    identity: Identity
    recorder: AuditRecorder
    keys: KeyStore
    # Binds CSRF tokens to warden sessions (spark.identity.web).
    csrf_key: bytes
    _previous_recorder: AuditRecorder | None

    @classmethod
    async def start(
        cls, database: Database, settings: SettingsLike, secrets: SecretsLike
    ) -> IdentityRuntime:
        """Build the runtime over ``database`` (PostgreSQL) and konfig's settings and secrets."""
        base_url = public_base_url(settings.get("server.public_base_url", ""))
        config = warden_config(settings, secrets, base_url)
        wdb = warden_database(database)
        recorder = AuditRecorder(
            wdb, ActionRegistry(), details_max_bytes=config.audit.details_max_bytes
        )
        register_actions(recorder.registry)
        previous = set_recorder(recorder)
        try:
            keys = KeyStore(wdb, secrets, config.keys)
            csrf_key = await _bootstrap(wdb, keys, secrets, config)
            identity = Identity(config, wdb, keys, build_registry(), secrets=secrets)
        except BaseException:
            set_recorder(previous)
            raise
        return cls(config, identity, recorder, keys, csrf_key, previous)

    def close(self) -> None:
        """Put back the audit recorder that was installed before this runtime."""
        set_recorder(self._previous_recorder)

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Run warden's background work for a block (the app's lifespan), then close."""
        try:
            async with self.identity.running():
                yield
        finally:
            self.close()


async def _bootstrap(wdb: Any, keys: KeyStore, secrets: SecretsLike, config: WardenConfig) -> bytes:
    """Make sure a signing key, the TOTP key and the CSRF key exist; return the CSRF key.

    Serialised across replicas by a session-level advisory lock on one
    connection, released before returning.
    """
    async with wdb.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_BOOTSTRAP_LOCK,))
        try:
            await asyncio.to_thread(
                _ensure_secret, secrets, config.identity.totp_key_secret, new_totp_key
            )
            csrf = await asyncio.to_thread(
                _ensure_secret, secrets, CSRF_KEY_SECRET, lambda: _secrets.token_urlsafe(32)
            )
            try:
                await keys.sign(b"spark start-up check")
            except KeyStoreError as exc:
                logger.warning("No usable warden signing key (%s); rotating one in", exc)
                await keys.rotate()
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_BOOTSTRAP_LOCK,))
            await conn.commit()
    return csrf.encode("utf-8")
