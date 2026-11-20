"""``spark bootstrap``: a server's first run, with nobody at a keyboard (server mode only).

Run once as the schema owner after ``spark migrate``, before web and worker (the
deployment's one-off task, or Compose's ``migrate`` service). In one PostgreSQL
advisory lock, so two runs at once agree:

1. check the owner's connection (TLS where the URL asks for it) and every bootstrap
   setting (:func:`spark.bootstrap.settings.collect`), failing once with all problems;
2. create or update the restricted logins, grant them SPARK's and warden's
   privileges and verify them (:mod:`spark.db.roles`), when they are configured;
3. create the instance identity, warden's signing key, the TOTP key and the CSRF
   key, by the code web start-up runs (:meth:`spark.identity.runtime.IdentityRuntime.start`);
4. store the start-up ``mail.smtp`` values while the database holds none, audited;
5. add the Amazon Bedrock connection that uses the task role, when
   ``bootstrap.bedrock.region`` is set: once, never over an administrator's change
   or deletion (:mod:`spark.bootstrap.connections`), audited;
6. invite the first administrator through warden with ``administrator`` staged,
   sent by the SMTP sender, unless someone already holds or is invited to that
   role; warden sends inside its transaction, so a failed delivery writes nothing;
7. write the bootstrap record (:mod:`spark.migrations.versions.bootstrap_record`).

A later run finds the record and sends nothing; steps 2 to 5 are repeated, so a
changed login password or a new table after an upgrade is applied.
``resend_invitation`` asks warden to resend the first administrator's invitation
(``spark bootstrap --resend-invitation``). Nothing here logs a URL, a password, an
address or an invitation link.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg import AsyncConnection

import spark
from spark.bootstrap import connections as bootstrap_connections
from spark.bootstrap import settings as bootstrap_settings
from spark.bootstrap.settings import BootstrapSettings, BootstrapSettingsError, KonfigLike
from spark.connections.model import ConnectionInvalid
from spark.db import roles
from spark.db.engine import Database, Executor
from spark.identity.mail import SmtpSender
from spark.identity.permissions import ADMINISTRATOR
from spark.instance_settings.startup import StartupConfigError
from spark.mode import DatabaseSettings

__all__ = [
    "ACTOR",
    "BOOTSTRAP_LOCK",
    "BootstrapError",
    "Outcome",
    "administrator_present",
    "completed",
    "connect_reason",
    "run",
]

logger = logging.getLogger(__name__)

ACTOR: Final = "spark-bootstrap"
#: pg_advisory_lock key for ``spark bootstrap`` ("SPKB").
BOOTSTRAP_LOCK: Final = 0x53504B42
_TLS_MODES: Final = frozenset({"require", "verify-ca", "verify-full"})


class BootstrapError(StartupConfigError):
    """Bootstrap could not finish; the message names what failed and never a secret."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one run did, for the command's output."""

    logins: Mapping[str, str]
    mail_seeded: tuple[str, ...]
    invited: bool
    resent: bool
    already_complete: bool
    #: What the Bedrock step did: ``created``, ``exists``, ``declined``, or empty when off.
    bedrock: str = ""


def connect_reason(exc: BaseException) -> str:
    """Why a connection failed, in fixed words: never the URL, a host's reply or a password."""
    text = str(exc).lower()
    if "password authentication failed" in text or "authentication failed" in text:
        return "the login or password was refused"
    if "certificate" in text or "ssl" in text or "tls" in text:
        return "the TLS connection could not be verified"
    if "does not exist" in text:
        return "the database or login does not exist"
    if "timeout" in text or "timed out" in text:
        return "the connection timed out"
    return f"the connection failed ({type(exc).__name__})"


async def _connect(url: str, key: str) -> AsyncConnection[Any]:
    try:
        return await AsyncConnection.connect(url, autocommit=True, connect_timeout=30)
    except psycopg.Error as exc:
        raise BootstrapError(f"{bootstrap_settings.named(key)}: {connect_reason(exc)}") from None


async def _encryption_problem(conn: AsyncConnection[Any], url: str, key: str) -> str | None:
    """A problem when the URL asks for TLS but the connection is not encrypted."""
    query = parse_qs(urlsplit(url).query)
    sslmode = (query.get("sslmode") or [os.environ.get("PGSSLMODE", "")])[0].lower()
    if sslmode not in _TLS_MODES:
        return None
    cur = await conn.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
    row = await cur.fetchone()
    if row is None or not row[0]:
        return f"{bootstrap_settings.named(key)} asks for TLS, but the connection is not encrypted"
    return None


async def completed(database: Executor) -> bool:
    """Whether the bootstrap record exists."""
    return await database.fetch_value("SELECT 1 FROM bootstrap WHERE id = 1") is not None


async def administrator_present(conn: AsyncConnection[Any]) -> bool:
    """Whether an active user holds ``administrator``, or an invited one has it staged."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM warden.role_assignments a"
        " JOIN warden.users u ON u.id = a.user_id"
        " WHERE a.role = %(role)s AND u.status = 'active'"
        " AND (a.expires_at IS NULL OR a.expires_at > now()))"
        " OR EXISTS (SELECT 1 FROM warden.invitation_grants g"
        " JOIN warden.users u ON u.id = g.user_id"
        " WHERE g.role = %(role)s AND u.status = 'invited')",
        {"role": ADMINISTRATOR},
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _logins(
    conn: AsyncConnection[Any], found: BootstrapSettings, database: str
) -> dict[str, str]:
    logins = found.logins
    if logins is None:
        return {}
    changes: dict[str, str] = {}
    for login in (logins.web, logins.worker, logins.purger):
        if login is not None:
            changes[login.name] = await roles.ensure_login(conn, login)
    purger = logins.purger.name if logins.purger else None
    await roles.apply_grants(
        conn, database=database, web=logins.web.name, worker=logins.worker.name, purger=purger
    )
    problems = await roles.verify(
        conn, database=database, web=logins.web.name, worker=logins.worker.name, purger=purger
    )
    if problems:
        raise BootstrapError("the restricted logins are not as expected: " + "; ".join(problems))
    for login in (logins.web, logins.worker, logins.purger):
        if login is not None and not await roles.signs_in(login):
            raise BootstrapError(f"the login {login.name} cannot sign in with its URL")
    for name, change in changes.items():
        if change != "unchanged":
            logger.info("Database login %s: %s", name, change)
    return changes


async def _seed_mail(
    database: Database, settings: KonfigLike, seed: Mapping[str, Any]
) -> tuple[str, ...]:
    if not seed:
        return ()
    from spark.instance_settings.accessor import ServerSettings

    source = ServerSettings(database, settings)
    await source.reload()
    for key, value in seed.items():
        await source.set(key, value, actor=ACTOR)
    logger.info("Stored the start-up mail settings: %s", ", ".join(sorted(seed)))
    return tuple(sorted(seed))


async def _seed_bedrock(database: Database, seed: bootstrap_settings.BedrockSeed) -> str:
    try:
        result = await bootstrap_connections.seed_bedrock(database, seed, actor=ACTOR)
    except ConnectionInvalid as refused:
        raise BootstrapError(f"the Bedrock connection was refused: {refused}") from None
    if result == "created":
        logger.info("Added the Amazon Bedrock connection %s", bootstrap_connections.CONNECTION_ID)
    return result


async def _invite(identity: Any, found: BootstrapSettings) -> None:
    from warden.identity import InvitationError, MailError, StagedGrant

    administrator = found.administrator
    try:
        await identity.invitations.create(
            administrator.email,
            actor=ACTOR,
            display_name=administrator.display_name,
            grants=[StagedGrant(role=ADMINISTRATOR)],
        )
    except MailError:
        raise BootstrapError(
            "the first administrator's invitation could not be sent; nothing was written. "
            "Check the mail.smtp settings and run spark bootstrap again"
        ) from None
    except InvitationError:
        raise BootstrapError(
            f"{bootstrap_settings.named(bootstrap_settings.ADMIN_EMAIL)} already belongs to a "
            "user who is not an administrator; give them the role with "
            "spark identity create-admin, or name another address"
        ) from None
    logger.info("Invited the first administrator")


async def _resend(identity: Any, found: BootstrapSettings) -> None:
    from warden.identity import MailError, UserStatus

    user = await identity.users.get_by_email(found.administrator.email)
    if user is None or user.status != UserStatus.INVITED:
        raise BootstrapError(
            "there is no pending invitation for the first administrator to resend "
            "(it was accepted, or never sent)"
        )
    try:
        await identity.invitations.resend(user.id, actor=ACTOR)
    except MailError:
        raise BootstrapError(
            "the invitation could not be resent; check the mail.smtp settings"
        ) from None
    logger.info("Resent the first administrator's invitation")


async def _record(database: Database) -> None:
    await database.execute(
        "INSERT INTO bootstrap (id, completed_at, administrator_id, spark_version)"
        " VALUES (1, ?, NULL, ?) ON CONFLICT (id) DO NOTHING",
        (datetime.now(UTC).replace(tzinfo=None), spark.__version__),
    )


async def run(
    database: DatabaseSettings,
    settings: KonfigLike,
    secrets: Any,
    *,
    resend_invitation: bool = False,
    environ: Mapping[str, str] | None = None,
    smtp_context: Any = None,
) -> Outcome:
    """Bootstrap the server at ``database`` (the schema owner's settings, at head).

    ``secrets`` is konfig's secrets store (read and written: the identity keys).
    ``smtp_context`` replaces the system's certificate authorities for the
    invitation (tests). Raises :class:`BootstrapSettingsError` or
    :class:`BootstrapError`.
    """
    from spark.identity import audit as spark_audit
    from spark.identity.runtime import IdentityRuntime
    from spark.instance_settings import repository

    url = database.url or ""
    env = os.environ if environ is None else environ
    owner_tls = bootstrap_settings.tls_problem(bootstrap_settings.DATABASE_URL, url, env)
    if owner_tls:
        raise BootstrapSettingsError([owner_tls])
    lock = await _connect(url, bootstrap_settings.DATABASE_URL)
    try:
        await lock.execute("SELECT pg_advisory_lock(%s)", (BOOTSTRAP_LOCK,))
        encryption = await _encryption_problem(lock, url, bootstrap_settings.DATABASE_URL)
        if encryption:
            raise BootstrapSettingsError([encryption])
        db = await Database.open(database, max_size=4)
        try:
            stored = await repository.load_instance(db)
            stored_mail = {k: v for k, v in stored.items() if k.startswith("mail.smtp.")}
            found = bootstrap_settings.collect(
                settings, secrets, owner_url=url, stored_mail=stored_mail, environ=env
            )
            already = await completed(db)
            name = unquote(urlsplit(url).path.lstrip("/"))
            changes = await _logins(lock, found, name)
            sender = SmtpSender(
                bootstrap_settings.smtp_of(found.mail, secrets), context=smtp_context
            )
            identity = await IdentityRuntime.start(db, settings, secrets, mail_sender=sender)
            invited = resent = False
            bedrock = ""
            try:
                async with spark_audit.installed(db):
                    seeded = await _seed_mail(db, settings, found.mail_seed)
                    if found.bedrock is not None:
                        bedrock = await _seed_bedrock(db, found.bedrock)
                    if not already and not await administrator_present(lock):
                        await _invite(identity.identity, found)
                        invited = True
                    if resend_invitation:
                        await _resend(identity.identity, found)
                        resent = True
                if not already:
                    await _record(db)
            finally:
                identity.close()
        finally:
            await db.close()
    finally:
        try:
            await lock.execute("SELECT pg_advisory_unlock(%s)", (BOOTSTRAP_LOCK,))
        except psycopg.Error:
            pass
        await lock.close()
    return Outcome(changes, seeded, invited, resent, already, bedrock)
