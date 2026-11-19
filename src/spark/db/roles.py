"""Restricted PostgreSQL logins for web, worker and the audit purge (``spark bootstrap``).

A server keeps the schema owner for ``spark migrate`` and ``spark bootstrap`` and
gives web and worker their own logins, which own nothing, so the database itself
keeps warden's audit trail append-only (a table's owner bypasses grants). With an
audit retention the purge connects as a third login, the only one that may delete
audit rows (docs/deployment/database.md).

Each login's name and password come from the URL it connects with (one source per
password). As the owner, :func:`ensure_login` creates the login, or changes its
password when the URL's no longer signs in; :func:`apply_grants` grants SPARK's
schema and, through :func:`warden.audit.grants.append_only_grants`, warden's; and
:func:`verify` checks the result. Everything is safe to run again.

The password is sent to PostgreSQL only as a SCRAM verifier computed here
(``PQencryptPasswordConn``), so it never appears in a statement or the server's
log. Nothing here logs or returns a URL or a password; errors name the login.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg import AsyncConnection, sql
from warden.audit.grants import append_only_grants

__all__ = [
    "Login",
    "LoginChange",
    "RolesError",
    "apply_grants",
    "ensure_login",
    "login_of",
    "signs_in",
    "verify",
]

SPARK_SCHEMA: Final = "spark"
WARDEN_SCHEMA: Final = "warden"
_AUDIT_EVENTS: Final = "warden.audit_events"
_VERSION_TABLE: Final = "spark.alembic_version"
# A login name Spark creates: an ordinary lower-case PostgreSQL identifier.
_NAME: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")

LoginChange = Literal["created", "password changed", "unchanged"]


class RolesError(RuntimeError):
    """A login could not be created, granted or verified; the message names the login."""


@dataclass(frozen=True, slots=True)
class Login:
    """A login role and its password, read from the URL it connects with."""

    name: str
    password: str = field(repr=False)
    url: str = field(repr=False)


def login_of(url: str) -> Login:
    """The login a PostgreSQL URL connects as; :class:`ValueError` (no value in it) if unusable."""
    parts = urlsplit(url)
    name = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if not name:
        raise ValueError("the URL names no login")
    if not _NAME.fullmatch(name):
        raise ValueError("the login name must be lower-case letters, digits and underscores")
    if not password:
        raise ValueError("the URL carries no password")
    return Login(name, password, url)


async def _exists(conn: AsyncConnection[Any], name: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return await cur.fetchone() is not None


async def signs_in(login: Login) -> bool:
    """Whether ``login`` can connect with its URL now."""
    try:
        conn = await AsyncConnection.connect(login.url, autocommit=True, connect_timeout=10)
    except psycopg.OperationalError:
        return False
    await conn.close()
    return True


def _verifier(conn: AsyncConnection[Any], login: Login) -> str:
    encrypted = conn.pgconn.encrypt_password(login.password.encode(), login.name.encode())
    return encrypted.decode("ascii")


async def ensure_login(conn: AsyncConnection[Any], login: Login) -> LoginChange:
    """Create ``login``, or set its password when its URL no longer signs in.

    ``conn`` is the owner's connection, in autocommit. The owner needs
    ``CREATEROLE`` (and admin of an existing login, as its creator has).
    """
    ident = sql.Identifier(login.name)
    try:
        if not await _exists(conn, login.name):
            verifier = sql.Literal(_verifier(conn, login))
            await conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(ident, verifier))
            return "created"
        if await signs_in(login):
            return "unchanged"
        verifier = sql.Literal(_verifier(conn, login))
        await conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(ident, verifier))
    except psycopg.Error as exc:
        raise RolesError(
            f"could not create or update the login {login.name} ({type(exc).__name__}); "
            "the schema owner needs CREATEROLE"
        ) from None
    return "password changed"


def _spark_grants(database: str, apps: list[str], everyone: list[str]) -> list[sql.Composed]:
    app_roles = sql.SQL(", ").join(sql.Identifier(n) for n in apps)
    all_roles = sql.SQL(", ").join(sql.Identifier(n) for n in everyone)
    schema = sql.Identifier(SPARK_SCHEMA)
    return [
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), all_roles),
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, app_roles),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
            schema, app_roles
        ),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            schema, app_roles
        ),
        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {} FROM {}").format(
            sql.Identifier(SPARK_SCHEMA, "alembic_version"), app_roles
        ),
        # Tables and sequences later migrations create as this owner.
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON TABLES TO {}"
        ).format(schema, app_roles),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(schema, app_roles),
    ]


async def apply_grants(
    conn: AsyncConnection[Any],
    *,
    database: str,
    web: str,
    worker: str,
    purger: str | None = None,
) -> None:
    """Grant SPARK's schema to web and worker, and warden's through warden's own grants.

    warden's block for each role first revokes what the role holds on its tables,
    so this is safe to run after every migration. The purger gets warden's purger
    grants and nothing in SPARK's schema.
    """
    everyone = [web, worker] + ([purger] if purger else [])
    statements = _spark_grants(database, [web, worker], everyone)
    try:
        async with conn.transaction():
            for statement in statements:
                await conn.execute(statement)
            await conn.execute(append_only_grants(app_role=web))
            await conn.execute(append_only_grants(app_role=worker, purger_role=purger))
    except psycopg.Error as exc:
        raise RolesError(
            f"could not grant {', '.join(everyone)} their privileges ({type(exc).__name__})"
        ) from None


async def _value(conn: AsyncConnection[Any], query: str, *params: Any) -> Any:
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    return None if row is None else row[0]


async def _owns_nothing(conn: AsyncConnection[Any], name: str) -> bool:
    owned = await _value(
        conn,
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
        "WHERE r.rolname = %s",
        name,
    )
    return int(owned or 0) == 0


async def verify(
    conn: AsyncConnection[Any],
    *,
    database: str,
    web: str,
    worker: str,
    purger: str | None = None,
) -> list[str]:
    """Check each login holds exactly what it should; return the problems (empty when right)."""
    problems: list[str] = []

    async def table(role: str, table_name: str, privilege: str) -> bool:
        return bool(
            await _value(
                conn, "SELECT has_table_privilege(%s, %s, %s)", role, table_name, privilege
            )
        )

    for role in (web, worker):
        if await _value(conn, "SELECT rolsuper FROM pg_roles WHERE rolname = %s", role):
            problems.append(f"{role} is a superuser")
        if await _value(conn, "SELECT has_database_privilege(%s, %s, 'CREATE')", role, database):
            problems.append(f"{role} may create schemas in the database")
        if not await _owns_nothing(conn, role):
            problems.append(f"{role} owns tables, which bypasses grants")
        if not await table(role, "spark.instance_settings", "SELECT"):
            problems.append(f"{role} cannot read Spark's tables")
        if await table(role, _VERSION_TABLE, "INSERT"):
            problems.append(f"{role} may write Spark's migration version")
        if not await table(role, _AUDIT_EVENTS, "INSERT"):
            problems.append(f"{role} cannot append to the audit trail")
        for privilege in ("UPDATE", "DELETE"):
            if await table(role, _AUDIT_EVENTS, privilege):
                problems.append(f"{role} may {privilege} audit rows")
    if purger is not None:
        if not await table(purger, _AUDIT_EVENTS, "DELETE"):
            problems.append(f"{purger} cannot delete purged audit rows")
        if await table(purger, _AUDIT_EVENTS, "UPDATE"):
            problems.append(f"{purger} may UPDATE audit rows")
        if await _value(conn, "SELECT has_schema_privilege(%s, %s, 'USAGE')", purger, SPARK_SCHEMA):
            problems.append(f"{purger} may use Spark's schema")
        if not await _owns_nothing(conn, purger):
            problems.append(f"{purger} owns tables, which bypasses grants")
    return problems
