"""Tests for a database owned by a process with no event loop of its own (tray, daemon)."""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from pathlib import Path

import pytest

from spark.database import conversations
from spark.db import migrate
from spark.db.migrate import MigrationRequired
from spark.db.standalone import open_standalone
from spark.mode import DatabaseSettings, Dialect

FIXTURES = Path(__file__).parent / "fixtures"


def test_opens_migrates_and_bridges(tmp_path: Path):
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    handle = open_standalone(settings, auto_migrate=True)
    try:
        cid = handle.sync_db.call(conversations.create_conversation, "c", "m", "u")
        assert handle.sync_db.call(conversations.get_conversation, cid, "u")["name"] == "c"
        assert handle.loop_thread_name in {t.name for t in threading.enumerate()}
    finally:
        handle.close()
    assert handle.loop_thread_name not in {t.name for t in threading.enumerate()}
    migrate.check_at_head(settings)


def test_upgrades_a_legacy_database_in_personal_mode(tmp_path: Path):
    path = tmp_path / "spark.db"
    shutil.copy(FIXTURES / "spark_0_2_0.db", path)
    handle = open_standalone(DatabaseSettings(Dialect.SQLITE, sqlite_path=path), auto_migrate=True)
    try:
        assert handle.sync_db.call(conversations.get_active_conversations, "user-example-0001")
    finally:
        handle.close()


def test_refuses_a_database_behind_head_without_auto_migrate(tmp_path: Path):
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    before = {t.name for t in threading.enumerate()}
    with pytest.raises(MigrationRequired):
        open_standalone(settings, auto_migrate=False)
    assert {t.name for t in threading.enumerate()} <= before  # no loop thread left running


def test_close_is_bounded_and_reports_what_it_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A database close that never finishes gives up within close_timeout, logs what was
    still pending on the loop, cancels it and stops the loop thread (decision 234)."""
    handle = open_standalone(
        DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db"), auto_migrate=True
    )
    database = handle.sync_db.database
    real_close = database.close

    async def a_close_that_never_finishes() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(database, "close", a_close_that_never_finishes)
    handle.close_timeout = 0.5
    started = time.perf_counter()
    try:
        with caplog.at_level("WARNING", logger="spark.db"):
            with pytest.raises(TimeoutError):
                handle.close()
        assert time.perf_counter() - started < 5
        closing = [r.getMessage() for r in caplog.records if r.name == "spark.db.standalone"]
        assert len(closing) == 1
        assert "did not close within 0.5 s" in closing[0]
        assert "a_close_that_never_finishes" in closing[0]  # what was pending
        stopping = [r.getMessage() for r in caplog.records if r.name == "spark.db.loop"]
        assert any("a_close_that_never_finishes" in m for m in stopping)  # what was cancelled
        assert handle.loop_thread_name not in {t.name for t in threading.enumerate()}
    finally:
        # Close the real connections (and their worker threads) on a loop of our own.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(real_close())
        finally:
            loop.close()


def test_close_is_idempotent(tmp_path: Path):
    handle = open_standalone(
        DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db"), auto_migrate=True
    )
    handle.close()
    handle.close()
