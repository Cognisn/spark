"""Scope resolution truth table."""

from __future__ import annotations

import pytest

from spark.database import Database, conversations
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.knowledge import store
from spark.knowledge.resolve import available_scopes, conversation_kg_settings


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)
    return conn


def _conv(db, *, local=0, use_global=1) -> int:
    cid = conversations.create_conversation(db, "c", "m", "u1")
    ph = db.placeholder
    db.execute(
        f"UPDATE conversations SET kg_local_enabled = {ph}, kg_use_global = {ph} "
        f"WHERE id = {ph}",
        (local, use_global, cid),
    )
    db.commit()
    return cid


def _seed(db, scope) -> None:
    store.upsert_node(db, scope, f"seed-{scope}", "concept", "", None, "u1")


class TestResolution:
    def test_global_only_by_default(self, db) -> None:
        cid = _conv(db)
        _seed(db, "global")
        assert available_scopes(db, cid, "u1") == ["global"]

    def test_global_disabled(self, db) -> None:
        cid = _conv(db, use_global=0)
        _seed(db, "global")
        assert available_scopes(db, cid, "u1") == []

    def test_local_first_when_built(self, db) -> None:
        cid = _conv(db, local=1)
        _seed(db, f"conv:{cid}")
        _seed(db, "global")
        assert available_scopes(db, cid, "u1") == [f"conv:{cid}", "global"]

    def test_local_enabled_but_unbuilt_not_listed(self, db) -> None:
        cid = _conv(db, local=1)
        _seed(db, "global")
        assert available_scopes(db, cid, "u1") == ["global"]

    def test_linked_conversations_pool(self, db) -> None:
        from spark.database import conversation_links

        a = _conv(db, local=1)
        b = _conv(db, local=1)
        _seed(db, f"conv:{a}")
        _seed(db, f"conv:{b}")
        conversation_links.add_link(db, a, b, "u1")
        scopes = available_scopes(db, a, "u1")
        assert f"conv:{a}" in scopes and f"conv:{b}" in scopes

    def test_no_conversation_context(self, db) -> None:
        assert available_scopes(db, None, "u1") == []
        _seed(db, "global")
        assert available_scopes(db, None, "u1") == ["global"]


class TestSettings:
    def test_settings_read(self, db) -> None:
        cid = _conv(db, local=1, use_global=0)
        s = conversation_kg_settings(db, cid, "u1")
        assert s == {"kg_local_enabled": True, "kg_use_global": False, "kg_auto_context": True}

    def test_missing_conversation_defaults(self, db) -> None:
        s = conversation_kg_settings(db, 9999, "u1")
        assert s == {"kg_local_enabled": False, "kg_use_global": True, "kg_auto_context": True}
