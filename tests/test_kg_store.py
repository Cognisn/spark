"""Tests for knowledge graph storage."""

from __future__ import annotations

import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


class TestKgSchema:
    def test_tables_exist(self, db) -> None:
        for table in ("kg_nodes", "kg_edges", "kg_builds"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    def test_conversation_kg_columns(self, db) -> None:
        ph = db.placeholder
        db.execute(
            f"INSERT INTO conversations (name, model_id, user_guid) VALUES ({ph}, {ph}, {ph})",
            ("t", "m", "u"),
        )
        db.commit()
        row = db.execute(
            "SELECT kg_local_enabled, kg_use_global, kg_auto_context FROM conversations"
        ).fetchone()
        assert tuple(row) == (0, 1, 1)
