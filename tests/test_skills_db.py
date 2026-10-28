"""Tests for skills state persistence."""

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


class TestSkillsSchema:
    def test_tables_exist(self, db) -> None:
        for table in ("skills", "conversation_skills"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


class TestSkillStates:
    def test_global_upsert_and_read(self, db) -> None:
        from spark.database import skills as skills_db

        skills_db.set_skill_enabled(db, "pdf-filler", False, "u1")
        skills_db.set_skill_enabled(db, "pdf-filler", True, "u1")
        assert skills_db.get_skill_states(db, "u1") == {"pdf-filler": True}
        assert skills_db.get_skill_states(db, "other") == {}

    def test_conversation_upsert_and_read(self, db) -> None:
        from spark.database import skills as skills_db

        skills_db.set_conversation_skill_enabled(db, 7, "pdf-filler", False)
        skills_db.set_conversation_skill_enabled(db, 7, "pdf-filler", True)
        assert skills_db.get_conversation_skill_states(db, 7) == {"pdf-filler": True}

    def test_resolve_enabled_defaults_on_and_layers(self, db) -> None:
        from spark.database import skills as skills_db

        class StubManager:
            def list_skills(self, include_invalid: bool = False):
                return [
                    {"name": "a", "description": "A", "valid": True},
                    {"name": "b", "description": "B", "valid": True},
                ]

        m = StubManager()
        # No rows: both enabled by default
        assert [s["name"] for s in skills_db.resolve_enabled(db, m, "u1")] == ["a", "b"]
        # Global disable removes it everywhere
        skills_db.set_skill_enabled(db, "a", False, "u1")
        assert [s["name"] for s in skills_db.resolve_enabled(db, m, "u1")] == ["b"]
        # Conversation disable layers on top
        skills_db.set_conversation_skill_enabled(db, 9, "b", False)
        assert skills_db.resolve_enabled(db, m, "u1", conversation_id=9) == []
        assert [s["name"] for s in skills_db.resolve_enabled(db, m, "u1")] == ["b"]
