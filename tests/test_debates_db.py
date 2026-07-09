"""Tests for the debate database schema and operations."""

from __future__ import annotations

import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    """Create a fresh test database and return its connection."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


class TestDebateSchema:
    def test_debate_tables_exist(self, db) -> None:
        for table in ("debate_config", "debate_agents", "debate_turns", "debate_exhibits"):
            cur = db.execute(f"SELECT COUNT(*) FROM {table}")
            assert cur.fetchone()[0] == 0

    def test_conversation_type_column(self, db) -> None:
        ph = db.placeholder
        db.execute(
            f"INSERT INTO conversations (name, model_id, user_guid) VALUES ({ph}, {ph}, {ph})",
            ("t", "m", "u"),
        )
        db.commit()
        cur = db.execute("SELECT conversation_type FROM conversations")
        assert cur.fetchone()[0] == "standard"

    def test_schema_initialise_idempotent(self, db) -> None:
        from spark.database.schema import initialise_schema

        initialise_schema(db)  # second run must not raise
        initialise_schema(db)  # third run must not raise
