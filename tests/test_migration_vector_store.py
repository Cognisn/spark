"""Revision vector_store_1: pgvector tables in server mode, nothing on SQLite.

Upgrade, backfill from the stored embeddings, and downgrade, on both engines.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "vector_store_1"
VECTOR_TABLES = {"memory_vectors", "context_vectors"}


def _sqlite(path: Path) -> DatabaseSettings:
    return DatabaseSettings(Dialect.SQLITE, sqlite_path=path)


def _pg(url: str) -> DatabaseSettings:
    return DatabaseSettings(Dialect.POSTGRESQL, url=url)


def _sqlite_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _pg_tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'spark'"
        ).fetchall()
    return {r[0] for r in rows}


def test_revision_follows_the_baseline() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == migrate.BASELINE


def test_sqlite_upgrade_and_downgrade_change_nothing(tmp_path: Path) -> None:
    path = tmp_path / "spark.db"
    migrate.upgrade(_sqlite(path), target=migrate.BASELINE)
    before = _sqlite_tables(path)

    migrate.upgrade(_sqlite(path), target=REVISION)
    assert _sqlite_tables(path) == before
    migrate.downgrade(_sqlite(path), target=migrate.BASELINE)
    assert _sqlite_tables(path) == before


@pytest.mark.postgres
def test_postgres_upgrade_creates_the_extension_and_tables(pg_url: str) -> None:
    migrate.upgrade(_pg(pg_url), target=REVISION)

    assert VECTOR_TABLES <= _pg_tables(pg_url)
    with psycopg.connect(pg_url) as conn:
        ext = conn.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert ext == ("vector",)


@pytest.mark.postgres
def test_postgres_upgrade_tolerates_an_existing_extension(pg_url: str) -> None:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")

    migrate.upgrade(_pg(pg_url), target=REVISION)

    assert VECTOR_TABLES <= _pg_tables(pg_url)


@pytest.mark.postgres
def test_postgres_downgrade_drops_the_tables_and_upgrade_restores_them(pg_url: str) -> None:
    migrate.upgrade(_pg(pg_url), target=REVISION)

    migrate.downgrade(_pg(pg_url), target=migrate.BASELINE)
    assert not VECTOR_TABLES & _pg_tables(pg_url)
    assert migrate.current(_pg(pg_url)) == migrate.BASELINE

    migrate.upgrade(_pg(pg_url), target=REVISION)
    assert VECTOR_TABLES <= _pg_tables(pg_url)


@pytest.mark.postgres
def test_postgres_upgrade_backfills_vectors_from_stored_embeddings(pg_url: str) -> None:
    migrate.upgrade(_pg(pg_url), target=migrate.BASELINE)
    memory = np.array([0.6, 0.8, 0.0], dtype=np.float32)
    element = np.array([0.0, 1.0], dtype=np.float32)
    with psycopg.connect(pg_url, options="-c search_path=spark,public") as conn:
        conn.execute(
            "INSERT INTO user_memories (user_guid, content, content_hash, embedding) "
            "VALUES ('user-alice-example', 'm', 'h1', %s), ('user-alice-example', 'none', 'h2', NULL)",
            (memory.tobytes(),),
        )
        cid = conn.execute(
            "INSERT INTO conversations (name, user_guid) VALUES ('c', 'user-alice-example') "
            "RETURNING id"
        ).fetchone()
        assert cid is not None
        conn.execute(
            "INSERT INTO context_index_elements (conversation_id, element_type, content_hash, "
            "content_text, embedding, user_guid) VALUES (%s, 'user', 'h', 't', %s, 'u')",
            (cid[0], element.tobytes()),
        )

    migrate.upgrade(_pg(pg_url), target=REVISION)

    with psycopg.connect(pg_url, options="-c search_path=spark,public") as conn:
        mem_rows = conn.execute("SELECT dimension, embedding::text FROM memory_vectors").fetchall()
        ctx_rows = conn.execute("SELECT dimension, embedding::text FROM context_vectors").fetchall()
    assert mem_rows == [(3, "[0.6,0.8,0]")]
    assert ctx_rows == [(2, "[0,1]")]
