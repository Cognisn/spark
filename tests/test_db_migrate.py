"""Tests for the Alembic chain, the baseline, the legacy SQLite upgrade and the head checks.

The baseline must reproduce the SPARK 0.2.0 schema exactly. The reference is
``tests/fixtures/spark_0_2_0.db``, created by the v0.2.0 tag's own
``initialise_schema`` (see tests/fixtures/README.md); ``spark_0_1_0.db`` was
created the same way at v0.1.0 and exercises the 0.2.0 ALTERs.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark.db import migrate
from spark.db.migrate import (
    BASELINE,
    Chain,
    DatabaseTooNew,
    LegacySchemaMismatch,
    MigrationRequired,
)
from spark.migrations import schema_0_2_0
from spark.migrations.baseline import table_names
from spark.mode import DatabaseSettings, Dialect

FIXTURES = Path(__file__).parent / "fixtures"


def _sqlite(path: Path) -> DatabaseSettings:
    return DatabaseSettings(Dialect.SQLITE, sqlite_path=path)


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copy(FIXTURES / name, target)
    return target


# -- Independent SQLite introspection (does not reuse production code) ----------


def _baseline_only(schema: dict[str, Any]) -> dict[str, Any]:
    """The baseline's tables of a schema (later revisions add their own tables)."""
    return {t: spec for t, spec in schema.items() if t in table_names()}


def _sqlite_schema(path: Path, *, ordered: bool = True) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
            )
        ]
        out: dict[str, Any] = {}
        for t in tables:
            cols = [tuple(r[1:6]) for r in conn.execute(f"PRAGMA table_xinfo({t})")]
            if not ordered:
                cols = sorted(cols)
            fks = sorted(tuple(r[2:5]) for r in conn.execute(f"PRAGMA foreign_key_list({t})"))
            indexes = []
            for idx in conn.execute(f"PRAGMA index_list({t})"):
                name, unique, origin = idx[1], idx[2], idx[3]
                columns = [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')")]
                indexes.append((name if origin == "c" else origin, unique, tuple(columns)))
            out[t] = {"columns": cols, "fks": fks, "indexes": sorted(indexes)}
        return out
    finally:
        conn.close()


def _alembic_version(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_extra_chains() -> Iterator[None]:
    saved = migrate.registered_chains()
    migrate.clear_chains()
    yield
    migrate.clear_chains()
    for chain in saved:
        migrate.register_chain(chain)


# -- SQLite --------------------------------------------------------------------


class TestSqliteFresh:
    def test_upgrade_creates_the_schema_at_head(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        report = migrate.upgrade(_sqlite(path))
        assert report.legacy_upgraded is False
        assert report.to_revision == migrate.head()
        assert _alembic_version(path) == [migrate.head()]
        assert set(_sqlite_schema(path)) >= set(table_names())
        assert len(table_names()) == len(schema_0_2_0.TABLES)

    def test_baseline_reproduces_the_0_2_0_schema_exactly(self, tmp_path: Path):
        """Tables, columns (in order, with types, NOT NULL, defaults, keys), FKs and indices."""
        built = tmp_path / "baseline.db"
        migrate.upgrade(_sqlite(built), target=BASELINE)
        reference = _sqlite_schema(FIXTURES / "spark_0_2_0.db")
        assert _sqlite_schema(built) == reference

    def test_upgrade_is_idempotent(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        migrate.upgrade(_sqlite(path))
        report = migrate.upgrade(_sqlite(path))
        assert report.from_revision == report.to_revision == migrate.head()

    def test_downgrade_to_base_removes_everything_and_upgrade_restores_it(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        migrate.upgrade(_sqlite(path))
        migrate.downgrade(_sqlite(path), target="base")
        assert _sqlite_schema(path) == {}
        assert _alembic_version(path) == []
        migrate.upgrade(_sqlite(path), target=BASELINE)
        assert _sqlite_schema(path) == _sqlite_schema(FIXTURES / "spark_0_2_0.db")


class TestLegacyUpgrade:
    def test_0_2_0_database_is_stamped_and_keeps_its_data(self, tmp_path: Path):
        path = _copy_fixture("spark_0_2_0.db", tmp_path)
        before = _sqlite_schema(path)
        report = migrate.upgrade(_sqlite(path))
        assert report.legacy_upgraded is True
        assert report.from_revision is None
        assert _alembic_version(path) == [migrate.head()]
        assert _baseline_only(_sqlite_schema(path)) == before
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
            assert conn.execute("SELECT name FROM conversations").fetchone()[0] == (
                "Synthetic planning chat"
            )
        finally:
            conn.close()

    def test_0_1_0_database_gains_the_0_2_0_columns_and_tables(self, tmp_path: Path):
        path = _copy_fixture("spark_0_1_0.db", tmp_path)
        report = migrate.upgrade(_sqlite(path))
        assert report.legacy_upgraded is True
        # ALTERed columns are appended, so compare ignoring column order.
        assert _baseline_only(_sqlite_schema(path, ordered=False)) == _sqlite_schema(
            FIXTURES / "spark_0_2_0.db", ordered=False
        )
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT rag_enabled, conversation_type, kg_auto_context FROM conversations"
            ).fetchone()
            assert row == (1, "standard", 1)
        finally:
            conn.close()

    def test_a_column_with_the_wrong_type_is_refused_and_nothing_changes(self, tmp_path: Path):
        path = _copy_fixture("spark_0_1_0.db", tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE conversations ADD COLUMN kg_use_global TEXT DEFAULT 'yes'")
        conn.commit()
        conn.close()
        before = _sqlite_schema(path)
        with pytest.raises(LegacySchemaMismatch) as exc:
            migrate.upgrade(_sqlite(path))
        message = str(exc.value)
        assert "conversations.kg_use_global" in message
        assert _sqlite_schema(path) == before
        assert not _has_table(path, "alembic_version")

    def test_an_unknown_column_is_refused(self, tmp_path: Path):
        path = _copy_fixture("spark_0_2_0.db", tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE messages ADD COLUMN mystery TEXT")
        conn.commit()
        conn.close()
        with pytest.raises(LegacySchemaMismatch, match="messages.mystery"):
            migrate.upgrade(_sqlite(path))

    def test_a_missing_column_is_refused(self, tmp_path: Path):
        path = _copy_fixture("spark_0_2_0.db", tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE voice_usage DROP COLUMN model_id")
        conn.commit()
        conn.close()
        # The 0.2.0 statements do not re-add it (no ALTER for it), so the check must catch it.
        with pytest.raises(LegacySchemaMismatch, match="voice_usage.model_id"):
            migrate.upgrade(_sqlite(path))

    def test_an_index_on_the_wrong_columns_is_refused(self, tmp_path: Path):
        path = _copy_fixture("spark_0_2_0.db", tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("DROP INDEX idx_messages_timestamp")
        conn.execute("CREATE INDEX idx_messages_timestamp ON messages(role)")
        conn.commit()
        conn.close()
        with pytest.raises(LegacySchemaMismatch, match="idx_messages_timestamp"):
            migrate.upgrade(_sqlite(path))


class TestHeadChecks:
    def test_empty_database_is_behind_head(self, tmp_path: Path):
        with pytest.raises(MigrationRequired, match="spark migrate"):
            migrate.check_at_head(_sqlite(tmp_path / "empty.db"))

    def test_unmigrated_legacy_database_is_behind_head(self, tmp_path: Path):
        with pytest.raises(MigrationRequired):
            migrate.check_at_head(_sqlite(_copy_fixture("spark_0_2_0.db", tmp_path)))

    def test_database_at_head_passes(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        migrate.upgrade(_sqlite(path))
        migrate.check_at_head(_sqlite(path))

    def test_database_below_head_is_refused(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "spark.db"
        migrate.upgrade(_sqlite(path))
        monkeypatch.setattr(migrate, "head", lambda: "9999_future")
        with pytest.raises(MigrationRequired, match="9999_future"):
            migrate.check_at_head(_sqlite(path))

    def test_database_newer_than_the_code_is_refused(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        migrate.upgrade(_sqlite(path))
        conn = sqlite3.connect(path)
        conn.execute("UPDATE alembic_version SET version_num = 'ffff_from_the_future'")
        conn.commit()
        conn.close()
        with pytest.raises(DatabaseTooNew, match="ffff_from_the_future"):
            migrate.check_at_head(_sqlite(path))
        with pytest.raises(DatabaseTooNew):
            migrate.upgrade(_sqlite(path))


class TestPrepare:
    def test_personal_mode_migrates_automatically(self, tmp_path: Path):
        path = _copy_fixture("spark_0_2_0.db", tmp_path)
        migrate.prepare(_sqlite(path), auto_migrate=True)
        assert _alembic_version(path) == [migrate.head()]

    def test_server_mode_refuses_a_database_behind_head(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        with pytest.raises(MigrationRequired):
            migrate.prepare(_sqlite(path), auto_migrate=False)
        assert not _has_table(path, "alembic_version")


class TestChains:
    def test_spark_chain_runs_first_then_registered_chains(self, tmp_path: Path):
        path = tmp_path / "spark.db"
        calls: list[str] = []

        def warden_like(settings: DatabaseSettings) -> str:
            assert _alembic_version(path) == [migrate.head()]
            calls.append("warden")
            return "w1"

        migrate.register_chain(Chain(name="warden", upgrade=warden_like))
        results = migrate.migrate_all(_sqlite(path))
        assert calls == ["warden"]
        assert results == {"spark": migrate.head(), "warden": "w1"}

    def test_duplicate_chain_names_are_refused(self):
        migrate.register_chain(Chain(name="extra", upgrade=lambda s: None))
        with pytest.raises(ValueError, match="extra"):
            migrate.register_chain(Chain(name="extra", upgrade=lambda s: None))

    def test_spark_is_a_reserved_chain_name(self):
        with pytest.raises(ValueError, match="spark"):
            migrate.register_chain(Chain(name="spark", upgrade=lambda s: None))


def _has_table(path: Path, name: str) -> bool:
    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


# -- PostgreSQL ----------------------------------------------------------------


def _pg_schema(url: str, schema: str) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(url) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
                "AND table_name <> 'alembic_version' ORDER BY table_name",
                (schema,),
            )
        ]
        out: dict[str, Any] = {}
        for t in tables:
            cols = [
                (name, dtype, nullable, (default or "").replace(f"{schema}.", ""))
                for name, dtype, nullable, default in conn.execute(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (schema, t),
                )
            ]
            indexes = sorted(
                (name, definition.replace(f"{schema}.", ""))
                for name, definition in conn.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
                    "AND tablename = %s",
                    (schema, t),
                )
            )
            out[t] = {"columns": cols, "indexes": indexes}
        return out


def _pg(url: str) -> DatabaseSettings:
    return DatabaseSettings(Dialect.POSTGRESQL, url=url)


@pytest.mark.postgres
class TestPostgres:
    def test_upgrade_creates_the_spark_schema_and_its_version_table(self, pg_url: str):
        import psycopg

        migrate.upgrade(_pg(pg_url))
        with psycopg.connect(pg_url) as conn:
            version = conn.execute("SELECT version_num FROM spark.alembic_version").fetchall()
            public = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchone()
        assert version == [(migrate.head(),)]
        assert public == (0,)
        assert set(_pg_schema(pg_url, "spark")) >= set(table_names())

    def test_baseline_matches_the_sqlite_baseline_table_for_table(self, pg_url: str, tmp_path):
        """Same tables, same columns in the same order, same nullability, same indices."""
        migrate.upgrade(_pg(pg_url), target=BASELINE)
        lite = _sqlite_schema(FIXTURES / "spark_0_2_0.db")
        pg = _pg_schema(pg_url, "spark")
        assert sorted(pg) == sorted(lite)
        for table, spec in lite.items():
            lite_cols = [(c[0], "NO" if (c[2] or c[4]) else "YES") for c in spec["columns"]]
            pg_cols = [(c[0], c[2]) for c in pg[table]["columns"]]
            assert pg_cols == lite_cols, table
            named = {i[0] for i in spec["indexes"] if i[0].startswith("idx_")}
            assert named <= {name for name, _ in pg[table]["indexes"]}, table

    def test_types_are_mapped(self, pg_url: str):
        migrate.upgrade(_pg(pg_url), target=BASELINE)
        cols = {c[0]: c for c in _pg_schema(pg_url, "spark")["user_memories"]["columns"]}
        assert cols["embedding"][1] == "bytea"
        assert cols["importance"][1] == "double precision"
        assert cols["created_at"][1] == "timestamp without time zone"
        assert cols["id"][3].startswith("nextval(")

    def test_downgrade_and_upgrade_again(self, pg_url: str):
        migrate.upgrade(_pg(pg_url))
        migrate.downgrade(_pg(pg_url), target="base")
        assert _pg_schema(pg_url, "spark") == {}
        migrate.upgrade(_pg(pg_url))
        migrate.check_at_head(_pg(pg_url))

    def test_check_at_head(self, pg_url: str):
        with pytest.raises(MigrationRequired):
            migrate.check_at_head(_pg(pg_url))
        migrate.upgrade(_pg(pg_url))
        migrate.check_at_head(_pg(pg_url))

    def test_the_0_2_0_statements_never_built_a_postgres_schema(self, pg_url: str):
        """Why there is no PostgreSQL legacy upgrade: 0.2.0's DDL fails on PostgreSQL.

        0.2.0 ran every statement in one psycopg2 transaction; the first failure
        aborted it and the final commit rolled everything back.
        """
        import psycopg

        with psycopg.connect(pg_url) as conn:
            with pytest.raises(psycopg.errors.UndefinedObject, match="blob"):
                with conn.transaction():
                    for sql in schema_0_2_0.TABLES:
                        conn.execute(sql.replace("{auto}", "SERIAL PRIMARY KEY"))
            count = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchone()
        assert count == (0,)


class TestChainShape:
    def test_the_chain_has_one_head_starting_at_the_baseline(self):
        scripts = migrate._scripts()
        assert scripts.get_bases() == [BASELINE]
        assert [migrate.head()] == scripts.get_heads()

    def test_a_forked_chain_is_refused(self, monkeypatch):
        class Forked:
            def get_heads(self) -> list[str]:
                return ["0002_a", "0002_b"]

        monkeypatch.setattr(migrate, "_scripts", lambda: Forked())
        with pytest.raises(migrate.MigrationError, match="exactly one head"):
            migrate.head()
