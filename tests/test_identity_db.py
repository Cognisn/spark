"""warden over SPARK's psycopg pool, and audit writes that join SPARK's transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from spark.db import Database, DatabaseError
from spark.identity.db import warden_database
from spark.mode import DatabaseSettings, Dialect

ACTION = "spark.test.identity_audit"


async def test_warden_database_refuses_sqlite(tmp_path: Path) -> None:
    from spark.db import migrate

    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    migrate.upgrade(settings)
    db = await Database.open(settings)
    try:
        with pytest.raises(DatabaseError, match="PostgreSQL"):
            warden_database(db)
    finally:
        await db.close()


@pytest.fixture
def pg_settings(pg_url: str) -> DatabaseSettings:
    """A fresh PostgreSQL database with SPARK's and warden's chains at head."""
    from spark.db import migrate
    from spark.identity import migrations as identity_migrations

    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    migrate.upgrade(settings)
    assert identity_migrations.upgrade(settings) == identity_migrations.warden_head()
    return settings


@pytest.fixture
async def pg_db(pg_settings: DatabaseSettings) -> AsyncIterator[Database]:
    db = await Database.open(pg_settings)
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def recorder(pg_db: Database) -> Iterator[Any]:
    """An audit recorder over SPARK's pool, installed for the test and removed afterwards."""
    from warden.audit import ActionRegistry, AuditRecorder, Severity, set_recorder

    registry = ActionRegistry()
    registry.register(ACTION, severity=Severity.INFO, summary="identity audit test")
    installed = AuditRecorder(warden_database(pg_db), registry)
    previous = set_recorder(installed)
    try:
        yield installed
    finally:
        set_recorder(previous)


def _event() -> Any:
    from warden.audit import AuditEvent

    return AuditEvent(action=ACTION, outcome="success", actor="user-example-0001")


async def _audit_count(db: Database) -> int:
    return int(
        await db.fetch_value("SELECT COUNT(*) FROM warden.audit_events WHERE action = ?", (ACTION,))
    )


@pytest.mark.postgres
async def test_warden_database_uses_sparks_pool(pg_db: Database) -> None:
    wdb = warden_database(pg_db)
    assert wdb.pool is pg_db.pool
    async with wdb.connection() as conn:
        cur = await conn.execute("SELECT to_regclass('warden.users') IS NOT NULL")
        row = await cur.fetchone()
    assert row == (True,)


@pytest.mark.postgres
async def test_an_audit_write_commits_with_the_spark_transaction(
    pg_db: Database, recorder: Any
) -> None:
    from warden.audit import record_audit

    async with pg_db.transaction() as conn:
        await conn.execute(
            "INSERT INTO user_identities (user_guid, warden_user_id) VALUES (?, ?)",
            ("user-example-0001", "warden-example-0001"),
        )
        await record_audit(_event(), conn=conn.raw)
    assert await _audit_count(pg_db) == 1
    assert await pg_db.fetch_value("SELECT COUNT(*) FROM user_identities") == 1


@pytest.mark.postgres
async def test_an_audit_write_rolls_back_with_the_spark_transaction(
    pg_db: Database, recorder: Any
) -> None:
    from warden.audit import record_audit

    with pytest.raises(RuntimeError, match="roll back"):
        async with pg_db.transaction() as conn:
            await conn.execute(
                "INSERT INTO user_identities (user_guid, warden_user_id) VALUES (?, ?)",
                ("user-example-0001", "warden-example-0001"),
            )
            await record_audit(_event(), conn=conn.raw)
            raise RuntimeError("roll back")
    assert await _audit_count(pg_db) == 0
    assert await pg_db.fetch_value("SELECT COUNT(*) FROM user_identities") == 0
