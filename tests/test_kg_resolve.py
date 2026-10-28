"""Scope resolution truth table."""

from __future__ import annotations

from spark.database import conversations
from spark.knowledge import store
from spark.knowledge.resolve import available_scopes, conversation_kg_settings


async def _conv(db, *, local=0, use_global=1) -> int:
    cid = await conversations.create_conversation(db, "c", "m", "u1")
    await db.execute(
        "UPDATE conversations SET kg_local_enabled = ?, kg_use_global = ? WHERE id = ?",
        (bool(local), bool(use_global), cid),
    )
    return cid


async def _seed(db, scope) -> None:
    await store.upsert_node(db, scope, f"seed-{scope}", "concept", "", None, "u1")


class TestResolution:
    async def test_global_only_by_default(self, spark_db) -> None:
        db = spark_db
        cid = await _conv(db)
        await _seed(db, "global")
        assert await available_scopes(db, cid, "u1") == ["global"]

    async def test_global_disabled(self, spark_db) -> None:
        db = spark_db
        cid = await _conv(db, use_global=0)
        await _seed(db, "global")
        assert await available_scopes(db, cid, "u1") == []

    async def test_local_first_when_built(self, spark_db) -> None:
        db = spark_db
        cid = await _conv(db, local=1)
        await _seed(db, f"conv:{cid}")
        await _seed(db, "global")
        assert await available_scopes(db, cid, "u1") == [f"conv:{cid}", "global"]

    async def test_local_enabled_but_unbuilt_not_listed(self, spark_db) -> None:
        db = spark_db
        cid = await _conv(db, local=1)
        await _seed(db, "global")
        assert await available_scopes(db, cid, "u1") == ["global"]

    async def test_linked_conversations_pool(self, spark_db) -> None:
        from spark.database import conversation_links

        db = spark_db
        a = await _conv(db, local=1)
        b = await _conv(db, local=1)
        await _seed(db, f"conv:{a}")
        await _seed(db, f"conv:{b}")
        await conversation_links.add_link(db, a, b, "u1")
        scopes = await available_scopes(db, a, "u1")
        assert f"conv:{a}" in scopes and f"conv:{b}" in scopes

    async def test_no_conversation_context(self, spark_db) -> None:
        db = spark_db
        assert await available_scopes(db, None, "u1") == []
        await _seed(db, "global")
        assert await available_scopes(db, None, "u1") == ["global"]


class TestSettings:
    async def test_settings_read(self, spark_db) -> None:
        db = spark_db
        cid = await _conv(db, local=1, use_global=0)
        s = await conversation_kg_settings(db, cid, "u1")
        assert s == {"kg_local_enabled": True, "kg_use_global": False, "kg_auto_context": True}

    async def test_missing_conversation_defaults(self, spark_db) -> None:
        s = await conversation_kg_settings(spark_db, 9999, "u1")
        assert s == {"kg_local_enabled": False, "kg_use_global": True, "kg_auto_context": True}
