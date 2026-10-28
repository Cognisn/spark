"""SPARK's migrations: the Alembic chain, the one-time legacy SQLite upgrade and head checks.

On PostgreSQL SPARK's tables and its version table (``spark.alembic_version``)
live in the ``spark`` schema; on SQLite in the default schema.

* :func:`upgrade` brings a database to head. A SQLite database made by SPARK
  0.2.0 or earlier (tables but no version table) first gets the one-time legacy
  upgrade: 0.2.0's own statements and ALTERs, a check of every baseline column and
  index (refusing with :class:`LegacySchemaMismatch` and changing nothing on any
  difference), then a stamp at the baseline.
* :func:`check_at_head` refuses a database behind head (:class:`MigrationRequired`)
  or newer than this code (:class:`DatabaseTooNew`).
* :func:`prepare` is what start-up calls: personal mode migrates automatically,
  server mode only checks (``spark migrate`` runs the migrations there).
* :func:`migrate_all` is ``spark migrate``: SPARK's chain, then every chain
  registered with :func:`register_chain`. Wave 3 registers warden's chain there
  (warden runs its own Alembic chain in a ``warden`` schema).

Everything here is synchronous (Alembic is); call it with ``asyncio.to_thread``
from async code.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from spark.migrations.baseline import baseline_statements, legacy_statements, table_names
from spark.mode import DatabaseSettings, Dialect

__all__ = [
    "BASELINE",
    "Chain",
    "DatabaseTooNew",
    "LegacySchemaMismatch",
    "MigrationError",
    "MigrationRequired",
    "UpgradeReport",
    "check_at_head",
    "clear_chains",
    "current",
    "downgrade",
    "head",
    "migrate_all",
    "prepare",
    "register_chain",
    "registered_chains",
    "upgrade",
]

BASELINE = "0001"
SCHEMA = "spark"
_SCRIPTS = Path(__file__).resolve().parent.parent / "migrations"


class MigrationError(RuntimeError):
    """Base class for migration refusals."""


class MigrationRequired(MigrationError):
    """The database is behind this code's head revision."""


class DatabaseTooNew(MigrationError):
    """The database was migrated by a newer SPARK than this one."""


class LegacySchemaMismatch(MigrationError):
    """A pre-0.3.0 SQLite database does not match the 0.2.0 baseline after the legacy upgrade."""


@dataclass(frozen=True)
class UpgradeReport:
    from_revision: str | None
    to_revision: str
    legacy_upgraded: bool


@dataclass(frozen=True)
class Chain:
    """A further migration chain run by ``spark migrate`` after SPARK's own."""

    name: str
    upgrade: Callable[[DatabaseSettings], str | None]


_chains: list[Chain] = []


def register_chain(chain: Chain) -> None:
    """Add a chain after SPARK's (wave 3: warden's chain, in its ``warden`` schema)."""
    if chain.name == "spark":
        raise ValueError("'spark' is SPARK's own chain")
    if any(c.name == chain.name for c in _chains):
        raise ValueError(f"a chain named '{chain.name}' is already registered")
    _chains.append(chain)


def registered_chains() -> list[Chain]:
    return list(_chains)


def clear_chains() -> None:
    _chains.clear()


# -- Alembic plumbing ----------------------------------------------------------


def _engine(settings: DatabaseSettings) -> Engine:
    if settings.dialect is Dialect.SQLITE:
        if settings.sqlite_path is None:
            raise MigrationError("SQLite needs a database path")
        settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(f"sqlite:///{settings.sqlite_path}", poolclass=NullPool)
    if not settings.url:
        raise MigrationError("PostgreSQL needs a database URL")
    _, rest = settings.url.split("://", 1)
    return create_engine(
        f"postgresql+psycopg://{rest}",
        poolclass=NullPool,
        connect_args={"options": f"-c search_path={SCHEMA},public -c TimeZone=UTC"},
    )


def _version_schema(settings: DatabaseSettings) -> str | None:
    return SCHEMA if settings.dialect is Dialect.POSTGRESQL else None


def _config(connection: Connection, settings: DatabaseSettings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPTS))
    cfg.attributes["connection"] = connection
    cfg.attributes["version_schema"] = _version_schema(settings)
    return cfg


def _scripts() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPTS))
    return ScriptDirectory.from_config(cfg)


@contextmanager
def _connect(settings: DatabaseSettings) -> Iterator[Connection]:
    engine = _engine(settings)
    try:
        with engine.begin() as conn:
            if settings.dialect is Dialect.POSTGRESQL:
                conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            yield conn
    finally:
        engine.dispose()


def head() -> str:
    """This code's head revision."""
    heads = _scripts().get_heads()
    if len(heads) != 1:
        raise MigrationError(f"SPARK's chain must have exactly one head, found {heads}")
    return heads[0]


def _current(conn: Connection, settings: DatabaseSettings) -> str | None:
    ctx = MigrationContext.configure(conn, opts={"version_table_schema": _version_schema(settings)})
    return ctx.get_current_revision()


def _check_known(revision: str | None) -> None:
    if revision is None:
        return
    try:
        known = _scripts().get_revision(revision)
    except CommandError:
        known = None
    if known is None:
        raise DatabaseTooNew(
            f"the database is at revision {revision}, which this SPARK does not know "
            f"(its newest is {head()}); upgrade SPARK"
        )


def current(settings: DatabaseSettings) -> str | None:
    """The database's revision (None for an empty or legacy database)."""
    with _connect(settings) as conn:
        return _current(conn, settings)


# -- Legacy SQLite upgrade -------------------------------------------------------


def _is_legacy(conn: Connection, settings: DatabaseSettings) -> bool:
    if settings.dialect is not Dialect.SQLITE:
        return False
    tables = set(inspect(conn).get_table_names())
    return "alembic_version" not in tables and bool(tables & set(table_names()))


_ColumnSpec = tuple[str, int, Any, int]
_IndexSpec = tuple[int, tuple[str, ...]]


def _sqlite_spec(conn: sqlite3.Connection, tables: list[str]) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in tables:
        if table not in present:
            continue
        columns: dict[str, _ColumnSpec] = {
            r[1]: (str(r[2]).upper(), int(r[3]), r[4], int(r[5]))
            for r in conn.execute(f"PRAGMA table_xinfo({table})")
        }
        indexes: dict[str, _IndexSpec] = {}
        unique: set[tuple[str, ...]] = set()
        for idx in conn.execute(f"PRAGMA index_list({table})"):
            name, is_unique, origin = idx[1], int(idx[2]), idx[3]
            cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info('{name}')"))
            if origin == "c":
                indexes[name] = (is_unique, cols)
            elif origin == "u":
                unique.add(cols)
        spec[table] = {"columns": columns, "indexes": indexes, "unique": unique}
    return spec


def _expected_spec() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    try:
        for sql in baseline_statements("sqlite"):
            conn.execute(sql)
        return _sqlite_spec(conn, table_names())
    finally:
        conn.close()


def _differences(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for table, want in expected.items():
        have = actual.get(table)
        if have is None:
            problems.append(f"table {table} is missing")
            continue
        for column, spec in want["columns"].items():
            got = have["columns"].get(column)
            if got is None:
                problems.append(f"column {table}.{column} is missing")
            elif got != spec:
                problems.append(
                    f"column {table}.{column} is {_describe(got)}, expected {_describe(spec)}"
                )
        for column in have["columns"].keys() - want["columns"].keys():
            problems.append(f"column {table}.{column} is not part of SPARK 0.2.0")
        for name, spec in want["indexes"].items():
            got_index = have["indexes"].get(name)
            if got_index != spec:
                problems.append(f"index {name} on {table} does not match SPARK 0.2.0")
        for cols in want["unique"] - have["unique"]:
            problems.append(f"unique constraint on {table}({', '.join(cols)}) is missing")
    return problems


def _describe(spec: _ColumnSpec) -> str:
    col_type, notnull, default, pk = spec
    parts = [col_type or "untyped"]
    if notnull:
        parts.append("NOT NULL")
    if default is not None:
        parts.append(f"DEFAULT {default}")
    if pk:
        parts.append("PRIMARY KEY")
    return " ".join(parts)


def _legacy_upgrade(path: Path) -> None:
    """0.2.0's statements and ALTERs, then the baseline check, in one transaction."""
    statements, alters = legacy_statements()
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql in statements:
                conn.execute(sql)
            for sql in alters:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise
            problems = _differences(_sqlite_spec(conn, table_names()), _expected_spec())
            if problems:
                raise LegacySchemaMismatch(
                    "this database does not match the SPARK 0.2.0 schema, so it cannot be "
                    "upgraded automatically; nothing was changed. Differences: "
                    + "; ".join(sorted(problems))
                )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


# -- Public operations -----------------------------------------------------------


def upgrade(settings: DatabaseSettings, target: str = "head") -> UpgradeReport:
    """Bring the database to ``target`` (default head), upgrading a legacy SQLite file first."""
    legacy = False
    with _connect(settings) as conn:
        if _is_legacy(conn, settings):
            legacy = True
    if legacy:
        assert settings.sqlite_path is not None
        _legacy_upgrade(settings.sqlite_path)
        with _connect(settings) as conn:
            command.stamp(_config(conn, settings), BASELINE)
    with _connect(settings) as conn:
        before = _current(conn, settings)
        _check_known(before)
        command.upgrade(_config(conn, settings), target)
        after = _current(conn, settings)
    return UpgradeReport(
        from_revision=None if legacy else before,
        to_revision=after or "base",
        legacy_upgraded=legacy,
    )


def downgrade(settings: DatabaseSettings, target: str) -> None:
    """Step the database down to ``target`` (``"base"`` removes SPARK's tables)."""
    with _connect(settings) as conn:
        _check_known(_current(conn, settings))
        command.downgrade(_config(conn, settings), target)


def check_at_head(settings: DatabaseSettings) -> None:
    """Refuse a database that is not exactly at this code's head revision."""
    with _connect(settings) as conn:
        revision = _current(conn, settings)
    _check_known(revision)
    wanted = head()
    if revision != wanted:
        raise MigrationRequired(
            f"the database is at revision {revision or 'none'} but this SPARK needs "
            f"{wanted}; run `spark migrate`"
        )


def prepare(settings: DatabaseSettings, *, auto_migrate: bool) -> None:
    """Start-up: migrate automatically (personal mode) or only check (server mode)."""
    if auto_migrate:
        upgrade(settings)
    check_at_head(settings)


def migrate_all(settings: DatabaseSettings) -> dict[str, str | None]:
    """``spark migrate``: SPARK's chain, then each registered chain, in order."""
    results: dict[str, str | None] = {"spark": upgrade(settings).to_revision}
    for chain in _chains:
        results[chain.name] = chain.upgrade(settings)
    return results
