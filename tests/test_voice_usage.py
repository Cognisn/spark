"""Voice character accounting and the monthly cap."""

from __future__ import annotations

import pytest

from spark.database import Database, voice_usage
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.voice.usage import over_cap


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


class TestRecording:
    def test_characters_accrue(self, db) -> None:
        voice_usage.record(db, "u1", 100, conversation_id=7, model_id="m")
        voice_usage.record(db, "u1", 50, conversation_id=7, model_id="m")
        assert voice_usage.characters_this_month(db, "u1") == 150

    def test_scoped_by_user(self, db) -> None:
        voice_usage.record(db, "u1", 100)
        voice_usage.record(db, "u2", 999)
        assert voice_usage.characters_this_month(db, "u1") == 100

    def test_summary_groups_by_conversation(self, db) -> None:
        voice_usage.record(db, "u1", 10, conversation_id=1)
        voice_usage.record(db, "u1", 5, conversation_id=1)
        voice_usage.record(db, "u1", 20, conversation_id=2)
        summary = voice_usage.usage_summary(db, "u1")

        assert summary["characters_this_month"] == 35
        by_conv = {
            r["conversation_id"]: r["characters"] for r in summary["by_conversation"]
        }
        assert by_conv == {1: 15, 2: 20}


class TestCap:
    def test_unlimited_when_cap_is_zero(self, db) -> None:
        voice_usage.record(db, "u1", 1_000_000)
        assert over_cap(db, "u1", 0) is False

    def test_under_cap(self, db) -> None:
        voice_usage.record(db, "u1", 99)
        assert over_cap(db, "u1", 100) is False

    def test_at_cap_is_over(self, db) -> None:
        # At the boundary we stop: the next utterance would breach the cap.
        voice_usage.record(db, "u1", 100)
        assert over_cap(db, "u1", 100) is True

    def test_beyond_cap(self, db) -> None:
        voice_usage.record(db, "u1", 101)
        assert over_cap(db, "u1", 100) is True
