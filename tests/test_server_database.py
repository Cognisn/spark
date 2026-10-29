"""The web app opens its database in the lifespan, on the loop that serves it."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations
from spark.db import Database, SyncDatabase
from spark.db.migrate import MigrationRequired
from spark.mode import DatabaseSettings, Dialect
from spark.web.server import attach_database, create_app

FIXTURES = Path(__file__).parent / "fixtures"


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.settings.get = lambda key, default=None, **kw: {
        "interface.session_timeout_minutes": 60
    }.get(key, default)
    return ctx


def test_lifespan_opens_and_closes_the_database(tmp_path: Path):
    app = create_app(_ctx())
    attach_database(app, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db"), True)
    with TestClient(app):
        assert isinstance(app.state.database, Database)
        assert isinstance(app.state.sync_db, SyncDatabase)
        assert app.state.database_ready.is_set()
        cid = app.state.sync_db.call(conversations.create_conversation, "c", "m", "u")
        assert cid > 0
        database = app.state.database
    with pytest.raises(Exception):
        SyncDatabase(database).call(conversations.get_conversation, cid, "u")


def test_personal_mode_upgrades_a_0_2_0_database_at_start(tmp_path: Path):
    path = tmp_path / "spark.db"
    shutil.copy(FIXTURES / "spark_0_2_0.db", path)
    app = create_app(_ctx())
    attach_database(app, DatabaseSettings(Dialect.SQLITE, sqlite_path=path), True)
    with TestClient(app):
        rows = app.state.sync_db.call(conversations.get_active_conversations, "user-example-0001")
        assert [r["name"] for r in rows] == ["Synthetic planning chat"]


def test_server_mode_refuses_a_database_behind_head(tmp_path: Path):
    app = create_app(_ctx())
    attach_database(app, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db"), False)
    with pytest.raises(MigrationRequired):
        with TestClient(app):
            pass
    assert app.state.database_error is not None
    assert app.state.database_ready.is_set()  # waiters are released, and see the error


def test_no_database_attached_leaves_state_empty():
    app = create_app(_ctx())
    with TestClient(app):
        assert getattr(app.state, "database", None) is None
