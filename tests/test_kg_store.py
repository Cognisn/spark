"""Tests for knowledge graph storage."""

from __future__ import annotations

from spark.knowledge import store


class TestKgSchema:
    async def test_tables_exist(self, spark_db) -> None:
        for table in ("kg_nodes", "kg_edges", "kg_builds"):
            assert await spark_db.fetch_value(f"SELECT COUNT(*) FROM {table}") == 0

    async def test_conversation_kg_columns(self, spark_db) -> None:
        await spark_db.execute(
            "INSERT INTO conversations (name, model_id, user_guid) VALUES (?, ?, ?)",
            ("t", "m", "u"),
        )
        row = await spark_db.fetch_one(
            "SELECT kg_local_enabled, kg_use_global, kg_auto_context FROM conversations"
        )
        assert (row["kg_local_enabled"], row["kg_use_global"], row["kg_auto_context"]) == (
            0,
            1,
            1,
        )


class TestNodeMerge:
    async def test_upsert_creates_then_merges(self, spark_db) -> None:
        db = spark_db
        n1 = await store.upsert_node(
            db, "global", "Spark", "project", "AI research kit", None, "u1"
        )
        n2 = await store.upsert_node(
            db, "global", "spark", "project", "Updated description", None, "u1"
        )
        assert n1 == n2  # same name_key merges
        nodes = await store.get_nodes(db, "global", "u1")
        assert len(nodes) == 1
        assert nodes[0]["weight"] == 2 and nodes[0]["description"] == "Updated description"

    async def test_scopes_are_isolated(self, spark_db) -> None:
        db = spark_db
        await store.upsert_node(db, "global", "Spark", "project", "d", None, "u1")
        await store.upsert_node(db, "conv:7", "Spark", "project", "d", None, "u1")
        assert await store.count_nodes(db, "global", "u1") == 1
        assert await store.count_nodes(db, "conv:7", "u1") == 1


class TestEdgeMerge:
    async def test_edge_weight_accumulates(self, spark_db) -> None:
        db = spark_db
        a = await store.upsert_node(db, "global", "Matthew", "person", "", None, "u1")
        b = await store.upsert_node(db, "global", "Spark", "project", "", None, "u1")
        e1 = await store.upsert_edge(db, "global", a, b, "develops", "author of", "u1")
        e2 = await store.upsert_edge(db, "global", a, b, "Develops", "", "u1")
        assert e1 == e2
        edges = await store.get_edges(db, "global", "u1")
        assert len(edges) == 1 and edges[0]["weight"] == 2


class TestBuildsAndClear:
    async def test_watermark_upsert(self, spark_db) -> None:
        db = spark_db
        await store.set_watermark(db, "global", "conv:3", 120, "u1")
        await store.set_watermark(db, "global", "conv:3", 240, "u1")
        state = await store.get_build_state(db, "global", "u1")
        assert len(state) == 1 and state[0]["watermark"] == 240

    async def test_clear_scope(self, spark_db) -> None:
        db = spark_db
        a = await store.upsert_node(db, "conv:9", "X", "concept", "", None, "u1")
        b = await store.upsert_node(db, "conv:9", "Y", "concept", "", None, "u1")
        await store.upsert_edge(db, "conv:9", a, b, "relates_to", "", "u1")
        await store.set_watermark(db, "conv:9", "conv:9", 5, "u1")
        await store.clear_scope(db, "conv:9", "u1")
        assert await store.count_nodes(db, "conv:9", "u1") == 0
        assert await store.count_edges(db, "conv:9", "u1") == 0
        assert await store.get_build_state(db, "conv:9", "u1") == []
