"""The baseline schema (revision 0001) built from the frozen 0.2.0 statements.

A fresh 0.2.0 database was made by running every CREATE TABLE, every CREATE
INDEX, then the ALTERs; ALTERs for columns already in a CREATE TABLE failed and
were ignored. The baseline therefore runs the CREATE statements, the indices and
only the ALTERs that add a column, giving the same columns in the same order.

On PostgreSQL the types are mapped (0.2.0's statements never ran there:
``BLOB`` is not a PostgreSQL type): the auto-increment key becomes
``SERIAL PRIMARY KEY``, ``BLOB`` becomes ``BYTEA`` and ``REAL`` becomes
``DOUBLE PRECISION`` (SQLite's REAL is eight bytes).
"""

from __future__ import annotations

import re

from spark.migrations.schema_0_2_0 import ALTERS, INDICES, TABLES

__all__ = [
    "alter_target",
    "baseline_statements",
    "create_statements",
    "legacy_statements",
    "table_columns",
    "table_name",
    "table_names",
]

_SQLITE_AUTO = "INTEGER PRIMARY KEY AUTOINCREMENT"
_POSTGRES_AUTO = "SERIAL PRIMARY KEY"
_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")
_ALTER = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (\w+)")
_CONSTRAINT_WORDS = frozenset({"FOREIGN", "UNIQUE", "PRIMARY", "CONSTRAINT", "CHECK"})


def table_name(create_sql: str) -> str:
    match = _CREATE.search(create_sql)
    if match is None:
        raise ValueError("not a CREATE TABLE statement")
    return match.group(1)


def table_names() -> list[str]:
    return [table_name(sql) for sql in TABLES]


def table_columns(create_sql: str) -> list[str]:
    """Column names declared in a CREATE TABLE statement, in order."""
    body = create_sql[create_sql.index("(") + 1 : create_sql.rindex(")")]
    names = []
    for line in body.splitlines():
        word = line.strip().split(" ", 1)[0].rstrip(",")
        if word and word.upper() not in _CONSTRAINT_WORDS and not word.startswith("UNIQUE("):
            names.append(word)
    return names


def alter_target(alter_sql: str) -> tuple[str, str]:
    match = _ALTER.search(alter_sql)
    if match is None:
        raise ValueError("not an ALTER TABLE ... ADD COLUMN statement")
    return match.group(1), match.group(2)


def _for_dialect(sql: str, dialect: str) -> str:
    if dialect == "sqlite":
        return sql.replace("{auto}", _SQLITE_AUTO)
    if dialect == "postgresql":
        sql = sql.replace("{auto}", _POSTGRES_AUTO)
        sql = re.sub(r"\bBLOB\b", "BYTEA", sql)
        return re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    raise ValueError(f"unsupported dialect: {dialect}")


def create_statements(dialect: str) -> list[str]:
    return [_for_dialect(sql, dialect) for sql in TABLES]


def baseline_statements(dialect: str) -> list[str]:
    """Statements that build the 0.2.0 schema on an empty database."""
    declared = {table_name(sql): set(table_columns(sql)) for sql in TABLES}
    alters = []
    for sql in ALTERS:
        table, column = alter_target(sql)
        if column not in declared[table]:
            alters.append(_for_dialect(sql, dialect))
    return create_statements(dialect) + list(INDICES) + alters


def legacy_statements() -> tuple[list[str], list[str]]:
    """0.2.0's own start-up sequence for SQLite: (statements, ALTERs that may duplicate)."""
    return create_statements("sqlite") + list(INDICES), [_for_dialect(s, "sqlite") for s in ALTERS]
