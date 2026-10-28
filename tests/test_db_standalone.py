"""Tests for a database owned by a process with no event loop of its own (tray, daemon)."""

from __future__ import annotations

import shutil
import threading
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


def test_close_is_idempotent(tmp_path: Path):
    handle = open_standalone(
        DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db"), auto_migrate=True
    )
    handle.close()
    handle.close()
