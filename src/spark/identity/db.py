"""warden's database handle over SPARK's own psycopg pool.

warden never opens a pool of its own: it borrows SPARK's connections, so a
SPARK write and the audit event that records it can commit in one transaction.
To join a SPARK transaction, pass its driver connection to warden::

    async with database.transaction() as conn:
        ...  # SPARK's writes
        await record_audit(event, conn=conn.raw)
"""

from __future__ import annotations

import warden.db

from spark.db.engine import Database, DatabaseError
from spark.mode import Dialect

__all__ = ["warden_database"]


def warden_database(database: Database) -> warden.db.Database:
    """warden's handle on ``database``'s pool; PostgreSQL (server mode) only."""
    pool = database.pool
    if database.dialect is not Dialect.POSTGRESQL or pool is None:
        raise DatabaseError("warden needs PostgreSQL (server mode); SQLite has no warden schema")
    return warden.db.Database(pool)
