"""Tests for the SQL dialect helpers in spark.db.dialect."""

from __future__ import annotations

import pytest

from spark.db.dialect import insert_returning_id, to_driver_sql, upsert
from spark.mode import Dialect


class TestPlaceholders:
    def test_sqlite_is_unchanged(self):
        sql = "SELECT * FROM t WHERE a = ? AND b LIKE '%x%'"
        assert to_driver_sql(sql, Dialect.SQLITE) == sql

    def test_postgres_converts_qmarks(self):
        assert to_driver_sql("SELECT ? , ?", Dialect.POSTGRESQL) == "SELECT %s , %s"

    def test_postgres_escapes_percent_everywhere(self):
        assert (
            to_driver_sql("SELECT * FROM t WHERE b LIKE '%x%' AND a = ?", Dialect.POSTGRESQL)
            == "SELECT * FROM t WHERE b LIKE '%%x%%' AND a = %s"
        )

    def test_postgres_keeps_qmarks_inside_string_literals(self):
        assert (
            to_driver_sql("SELECT 'why?' , ? , 'it''s ?'", Dialect.POSTGRESQL)
            == "SELECT 'why?' , %s , 'it''s ?'"
        )

    def test_postgres_keeps_qmarks_inside_quoted_identifiers(self):
        assert to_driver_sql('SELECT "a?b" FROM t WHERE x = ?', Dialect.POSTGRESQL) == (
            'SELECT "a?b" FROM t WHERE x = %s'
        )

    def test_postgres_rejects_unterminated_literal(self):
        with pytest.raises(ValueError, match="unterminated"):
            to_driver_sql("SELECT 'oops", Dialect.POSTGRESQL)


class TestUpsert:
    def test_updates_non_conflict_columns(self):
        assert upsert("skills", ["user_guid", "name", "enabled"], ["user_guid", "name"]) == (
            "INSERT INTO skills (user_guid, name, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT (user_guid, name) DO UPDATE SET enabled = excluded.enabled"
        )

    def test_explicit_update_columns(self):
        sql = upsert("t", ["a", "b", "c"], ["a"], update=["c"])
        assert sql.endswith("DO UPDATE SET c = excluded.c")

    def test_nothing_to_update_does_nothing(self):
        assert upsert("t", ["a", "b"], ["a", "b"]) == (
            "INSERT INTO t (a, b) VALUES (?, ?) ON CONFLICT (a, b) DO NOTHING"
        )

    def test_extra_set_clauses(self):
        sql = upsert("t", ["a", "b"], ["a"], extra_set=["updated_at = CURRENT_TIMESTAMP"])
        assert sql.endswith("DO UPDATE SET b = excluded.b, updated_at = CURRENT_TIMESTAMP")

    @pytest.mark.parametrize("bad", ["t; DROP TABLE x", "1abc", "a b", ""])
    def test_identifiers_are_validated(self, bad):
        with pytest.raises(ValueError, match="identifier"):
            upsert(bad, ["a"], ["a"])
        with pytest.raises(ValueError, match="identifier"):
            upsert("t", [bad], ["a"])


class TestInsertReturningId:
    def test_builds_insert_with_returning(self):
        assert insert_returning_id("messages", ["role", "content"]) == (
            "INSERT INTO messages (role, content) VALUES (?, ?) RETURNING id"
        )

    def test_requires_columns(self):
        with pytest.raises(ValueError):
            insert_returning_id("messages", [])
