"""SQL dialect helpers shared by SQLite (personal mode) and PostgreSQL (server mode).

Repository SQL is written once, with ``?`` placeholders, in the subset both
engines accept (``ON CONFLICT ... DO UPDATE``, ``RETURNING``, ``excluded.``).
:func:`to_driver_sql` adapts it to the driver's paramstyle at execution time.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from spark.mode import Dialect

__all__ = ["insert_returning_id", "to_driver_sql", "upsert"]

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def to_driver_sql(sql: str, dialect: Dialect) -> str:
    """Convert ``?`` placeholders for the driver.

    SQLite (aiosqlite) takes ``?`` as written. psycopg uses ``%s``, so outside
    string literals and quoted identifiers each ``?`` becomes ``%s``, and every
    literal ``%`` is doubled because psycopg parses the whole statement.
    """
    if dialect is Dialect.SQLITE:
        return sql
    out: list[str] = []
    quote: str | None = None
    for ch in sql:
        if quote is not None:
            if ch == quote:
                quote = None
            out.append("%%" if ch == "%" else ch)
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    if quote is not None:
        raise ValueError("unterminated quoted string in SQL")
    return "".join(out)


def _check(names: Sequence[str]) -> None:
    for name in names:
        if not _IDENTIFIER.match(name):
            raise ValueError(f"invalid SQL identifier: {name!r}")


def upsert(
    table: str,
    columns: Sequence[str],
    conflict: Sequence[str],
    *,
    update: Sequence[str] | None = None,
    extra_set: Sequence[str] = (),
) -> str:
    """``INSERT ... ON CONFLICT (...) DO UPDATE`` (or ``DO NOTHING``).

    ``update`` defaults to every column not in ``conflict``. ``extra_set`` adds
    literal assignments (for example ``updated_at = CURRENT_TIMESTAMP``).
    """
    _check([table, *columns, *conflict, *(update or ())])
    if not columns or not conflict:
        raise ValueError("upsert needs columns and conflict columns")
    targets = [c for c in columns if c not in conflict] if update is None else list(update)
    head = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT ({', '.join(conflict)}) "
    )
    sets = [f"{c} = excluded.{c}" for c in targets] + list(extra_set)
    if not sets:
        return head + "DO NOTHING"
    return head + "DO UPDATE SET " + ", ".join(sets)


def insert_returning_id(table: str, columns: Sequence[str]) -> str:
    """``INSERT ... RETURNING id`` (SQLite 3.35 or later, and PostgreSQL)."""
    _check([table, *columns])
    if not columns:
        raise ValueError("insert needs at least one column")
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) RETURNING id"
    )
