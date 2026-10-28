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


class TestNodeMerge:
    def test_upsert_creates_then_merges(self, db) -> None:
        from spark.knowledge import store

        n1 = store.upsert_node(db, "global", "Spark", "project", "AI research kit", None, "u1")
        n2 = store.upsert_node(db, "global", "spark", "project", "Updated description", None, "u1")
        assert n1 == n2  # same name_key merges
        nodes = store.get_nodes(db, "global", "u1")
        assert len(nodes) == 1
        assert nodes[0]["weight"] == 2 and nodes[0]["description"] == "Updated description"

    def test_scopes_are_isolated(self, db) -> None:
        from spark.knowledge import store

        store.upsert_node(db, "global", "Spark", "project", "d", None, "u1")
        store.upsert_node(db, "conv:7", "Spark", "project", "d", None, "u1")
        assert store.count_nodes(db, "global", "u1") == 1
        assert store.count_nodes(db, "conv:7", "u1") == 1


class TestEdgeMerge:
    def test_edge_weight_accumulates(self, db) -> None:
        from spark.knowledge import store

        a = store.upsert_node(db, "global", "Matthew", "person", "", None, "u1")
        b = store.upsert_node(db, "global", "Spark", "project", "", None, "u1")
        e1 = store.upsert_edge(db, "global", a, b, "develops", "author of", "u1")
        e2 = store.upsert_edge(db, "global", a, b, "Develops", "", "u1")
        assert e1 == e2
        edges = store.get_edges(db, "global", "u1")
        assert len(edges) == 1 and edges[0]["weight"] == 2


class TestBuildsAndClear:
    def test_watermark_upsert(self, db) -> None:
        from spark.knowledge import store

        store.set_watermark(db, "global", "conv:3", 120, "u1")
        store.set_watermark(db, "global", "conv:3", 240, "u1")
        state = store.get_build_state(db, "global", "u1")
        assert len(state) == 1 and state[0]["watermark"] == 240

    def test_clear_scope(self, db) -> None:
        from spark.knowledge import store

        a = store.upsert_node(db, "conv:9", "X", "concept", "", None, "u1")
        b = store.upsert_node(db, "conv:9", "Y", "concept", "", None, "u1")
        store.upsert_edge(db, "conv:9", a, b, "relates_to", "", "u1")
        store.set_watermark(db, "conv:9", "conv:9", 5, "u1")
        store.clear_scope(db, "conv:9", "u1")
        assert store.count_nodes(db, "conv:9", "u1") == 0
        assert store.count_edges(db, "conv:9", "u1") == 0
        assert store.get_build_state(db, "conv:9", "u1") == []
