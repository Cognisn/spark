"""Tests for skills state persistence."""

from __future__ import annotations

import pytest


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


class TestSkillsSchema:
    async def test_tables_exist(self, db) -> None:
        for table in ("skills", "conversation_skills"):
            assert await db.fetch_value(f"SELECT COUNT(*) FROM {table}") == 0


class TestSkillStates:
    async def test_global_upsert_and_read(self, db) -> None:
        from spark.database import skills as skills_db

        await skills_db.set_skill_enabled(db, "pdf-filler", False, "u1")
        await skills_db.set_skill_enabled(db, "pdf-filler", True, "u1")
        assert await skills_db.get_skill_states(db, "u1") == {"pdf-filler": True}
        assert await skills_db.get_skill_states(db, "other") == {}

    async def test_conversation_upsert_and_read(self, db) -> None:
        from spark.database import skills as skills_db

        await skills_db.set_conversation_skill_enabled(db, 7, "pdf-filler", False)
        await skills_db.set_conversation_skill_enabled(db, 7, "pdf-filler", True)
        assert await skills_db.get_conversation_skill_states(db, 7) == {"pdf-filler": True}

    async def test_resolve_enabled_defaults_on_and_layers(self, db) -> None:
        from spark.database import skills as skills_db

        class StubManager:
            def list_skills(self, include_invalid: bool = False):
                return [
                    {"name": "a", "description": "A", "valid": True},
                    {"name": "b", "description": "B", "valid": True},
                ]

        m = StubManager()
        # No rows: both enabled by default
        assert [s["name"] for s in await skills_db.resolve_enabled(db, m, "u1")] == ["a", "b"]
        # Global disable removes it everywhere
        await skills_db.set_skill_enabled(db, "a", False, "u1")
        assert [s["name"] for s in await skills_db.resolve_enabled(db, m, "u1")] == ["b"]
        # Conversation disable layers on top
        await skills_db.set_conversation_skill_enabled(db, 9, "b", False)
        assert await skills_db.resolve_enabled(db, m, "u1", conversation_id=9) == []
        assert [s["name"] for s in await skills_db.resolve_enabled(db, m, "u1")] == ["b"]
