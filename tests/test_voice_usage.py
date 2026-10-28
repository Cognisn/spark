"""Voice character accounting and the monthly cap."""

from __future__ import annotations

import pytest

from spark.database import voice_usage
from spark.voice.usage import over_cap


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


class TestRecording:
    async def test_characters_accrue(self, db) -> None:
        await voice_usage.record(db, "u1", 100, conversation_id=7, model_id="m")
        await voice_usage.record(db, "u1", 50, conversation_id=7, model_id="m")
        assert await voice_usage.characters_this_month(db, "u1") == 150

    async def test_scoped_by_user(self, db) -> None:
        await voice_usage.record(db, "u1", 100)
        await voice_usage.record(db, "u2", 999)
        assert await voice_usage.characters_this_month(db, "u1") == 100

    async def test_summary_groups_by_conversation(self, db) -> None:
        await voice_usage.record(db, "u1", 10, conversation_id=1)
        await voice_usage.record(db, "u1", 5, conversation_id=1)
        await voice_usage.record(db, "u1", 20, conversation_id=2)
        summary = await voice_usage.usage_summary(db, "u1")

        assert summary["characters_this_month"] == 35
        by_conv = {r["conversation_id"]: r["characters"] for r in summary["by_conversation"]}
        assert by_conv == {1: 15, 2: 20}


class TestCap:
    async def test_unlimited_when_cap_is_zero(self, db) -> None:
        await voice_usage.record(db, "u1", 1_000_000)
        assert await over_cap(db, "u1", 0) is False

    async def test_under_cap(self, db) -> None:
        await voice_usage.record(db, "u1", 99)
        assert await over_cap(db, "u1", 100) is False

    async def test_at_cap_is_over(self, db) -> None:
        # At the boundary we stop: the next utterance would breach the cap.
        await voice_usage.record(db, "u1", 100)
        assert await over_cap(db, "u1", 100) is True

    async def test_beyond_cap(self, db) -> None:
        await voice_usage.record(db, "u1", 101)
        assert await over_cap(db, "u1", 100) is True
