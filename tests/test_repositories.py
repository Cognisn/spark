"""Repository behaviour added or made explicit by the async port, on SQLite and PostgreSQL."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from spark.database import autonomous_actions, conversations, memories, messages
from spark.db import Database

ALICE = "user-alice-example"
BOB = "user-bob-example"


class TestColumnAllowLists:
    async def test_update_conversation_refuses_unknown_columns(self, spark_db: Database):
        cid = await conversations.create_conversation(spark_db, "c", "m", ALICE)
        with pytest.raises(ValueError, match="id"):
            await conversations.update_conversation(spark_db, cid, ALICE, id=999)
        with pytest.raises(ValueError, match="unknown conversation fields"):
            await conversations.update_conversation(
                spark_db, cid, ALICE, **{"name = 'x', is_active": 0}
            )
        row = await conversations.get_conversation(spark_db, cid, ALICE)
        assert row is not None and row["name"] == "c"

    async def test_update_action_refuses_unknown_columns(self, spark_db: Database):
        aid = await autonomous_actions.create_action(spark_db, "a", "do it", "m", ALICE)
        with pytest.raises(ValueError, match="unknown action fields"):
            await autonomous_actions.update_action(spark_db, aid, ALICE, id=999)
        assert await autonomous_actions.get_action(spark_db, aid, ALICE) is not None

    async def test_known_columns_still_update(self, spark_db: Database):
        cid = await conversations.create_conversation(spark_db, "c", "m", ALICE)
        await conversations.update_conversation(spark_db, cid, ALICE, name="renamed", rag_top_k=9)
        row = await conversations.get_conversation(spark_db, cid, ALICE)
        assert row is not None and (row["name"], row["rag_top_k"]) == ("renamed", 9)


class TestUserScoping:
    async def test_conversations_are_invisible_to_other_users(self, spark_db: Database):
        cid = await conversations.create_conversation(spark_db, "Alice's", "m", ALICE)
        assert await conversations.get_conversation(spark_db, cid, BOB) is None
        assert await conversations.get_active_conversations(spark_db, BOB) == []
        assert await conversations.search_conversations(spark_db, "Alice", BOB) == []
        await conversations.update_conversation(spark_db, cid, BOB, name="hijacked")
        await conversations.delete_conversation(spark_db, cid, BOB)
        row = await conversations.get_conversation(spark_db, cid, ALICE)
        assert row is not None and row["name"] == "Alice's" and row["is_active"] == 1

    async def test_memories_are_per_user(self, spark_db: Database):
        emb = np.ones(4, dtype=np.float32)
        mid = await memories.add_memory(spark_db, ALICE, "likes tea", "facts", emb)
        assert mid is not None
        assert await memories.get_memory(spark_db, mid, BOB) is None
        assert await memories.search_memories(spark_db, BOB, emb) == []
        # The same content is a separate memory for another user.
        assert await memories.add_memory(spark_db, BOB, "likes tea", "facts", emb) is not None
        await memories.delete_all_memories(spark_db, BOB)
        assert await memories.get_memory(spark_db, mid, ALICE) is not None

    async def test_actions_are_per_user(self, spark_db: Database):
        aid = await autonomous_actions.create_action(spark_db, "nightly", "p", "m", ALICE)
        assert await autonomous_actions.get_action(spark_db, aid, BOB) is None
        assert await autonomous_actions.get_enabled_actions(spark_db, BOB) == []
        await autonomous_actions.delete_action(spark_db, aid, BOB)
        assert await autonomous_actions.get_action(spark_db, aid, ALICE) is not None


class TestParallelUnitsOfWork:
    async def test_concurrent_messages_keep_the_token_total_exact(self, spark_db: Database):
        """add_message inserts and bumps total_tokens in one transaction."""
        cid = await conversations.create_conversation(spark_db, "busy", "m", ALICE)
        await asyncio.gather(
            *(messages.add_message(spark_db, cid, "user", f"m{i}", 3, ALICE) for i in range(30))
        )
        row = await conversations.get_conversation(spark_db, cid, ALICE)
        assert row is not None and row["total_tokens"] == 90
        assert len(await messages.get_messages(spark_db, cid)) == 30

    async def test_only_one_daemon_wins_an_action_lock(self, spark_db: Database):
        aid = await autonomous_actions.create_action(spark_db, "once", "p", "m", ALICE)
        wins = await asyncio.gather(
            *(autonomous_actions.try_lock_action(spark_db, aid, f"daemon-{i}") for i in range(12))
        )
        assert sum(wins) == 1

    async def test_duplicate_memories_race_to_one_row(self, spark_db: Database):
        emb = np.ones(4, dtype=np.float32)
        ids = await asyncio.gather(
            *(memories.add_memory(spark_db, ALICE, "same fact", "facts", emb) for _ in range(10))
        )
        assert len([i for i in ids if i is not None]) == 1


class TestCompaction:
    async def test_apply_compaction_is_one_unit(self, spark_db: Database):
        cid = await conversations.create_conversation(spark_db, "long", "m", ALICE)
        ids = [
            await messages.add_message(spark_db, cid, "user", f"m{i}", 10, ALICE) for i in range(4)
        ]
        total = await messages.apply_compaction(
            spark_db,
            cid,
            "[COMPACTED] summary",
            5,
            ids[:3],
            ALICE,
            summary="summary",
            original_tokens=30,
        )
        assert total == 15  # the marker (5) and the one message left (10)
        active = await messages.get_messages(spark_db, cid)
        assert [m["content"] for m in active] == ["m3", "[COMPACTED] summary"]
        history = await spark_db.fetch_all(
            "SELECT original_message_count, summarised_token_count FROM rollup_history"
        )
        assert history == [{"original_message_count": 3, "summarised_token_count": 5}]

    async def test_a_failure_leaves_nothing_half_done(self, spark_db: Database, monkeypatch):
        cid = await conversations.create_conversation(spark_db, "long", "m", ALICE)
        ids = [
            await messages.add_message(spark_db, cid, "user", f"m{i}", 10, ALICE) for i in range(2)
        ]

        async def broken(*args, **kwargs):
            raise RuntimeError("recalculation failed")

        monkeypatch.setattr(conversations, "recalculate_total_tokens", broken)
        with pytest.raises(RuntimeError, match="recalculation failed"):
            await messages.apply_compaction(
                spark_db, cid, "[COMPACTED]", 5, ids, ALICE, summary="s", original_tokens=20
            )
        active = await messages.get_messages(spark_db, cid)
        assert [m["content"] for m in active] == ["m0", "m1"]
        assert await spark_db.fetch_value("SELECT COUNT(*) FROM rollup_history") == 0


class TestMemoryUpdates:
    async def test_update_content_re_hashes_and_stores_the_embedding(self, spark_db: Database):
        emb = np.ones(4, dtype=np.float32)
        mid = await memories.add_memory(spark_db, ALICE, "old", "facts", emb)
        new_emb = np.full(4, 2.0, dtype=np.float32)
        await memories.update_memory(
            spark_db, mid, ALICE, content="new", embedding=new_emb, category="projects"
        )
        mem = await memories.get_memory(spark_db, mid, ALICE)
        assert mem is not None
        assert (mem["content"], mem["category"]) == ("new", "projects")
        assert np.array_equal(mem["embedding"], new_emb)
        assert await memories.memory_exists(spark_db, ALICE, "new")
        assert not await memories.memory_exists(spark_db, ALICE, "old")

    async def test_update_without_content_keeps_it(self, spark_db: Database):
        mid = await memories.add_memory(spark_db, ALICE, "keep", "facts", np.ones(4))
        await memories.update_memory(spark_db, mid, ALICE, importance=0.9)
        mem = await memories.get_memory(spark_db, mid, ALICE)
        assert mem is not None and mem["content"] == "keep"
        assert mem["importance"] == pytest.approx(0.9)

    async def test_update_is_scoped_to_the_user(self, spark_db: Database):
        mid = await memories.add_memory(spark_db, ALICE, "mine", "facts", np.ones(4))
        await memories.update_memory(spark_db, mid, BOB, importance=0.1)
        mem = await memories.get_memory(spark_db, mid, ALICE)
        assert mem is not None and mem["importance"] == pytest.approx(0.5)

    async def test_content_without_embedding_is_refused(self, spark_db: Database):
        mid = await memories.add_memory(spark_db, ALICE, "x", "facts", np.ones(4))
        with pytest.raises(ValueError, match="embedding"):
            await memories.update_memory(spark_db, mid, ALICE, content="y")

    async def test_reassign_user_moves_only_that_users_memories(self, spark_db: Database):
        await memories.add_memory(spark_db, "default", "orphan one", "facts", np.ones(4))
        await memories.add_memory(spark_db, "default", "orphan two", "facts", np.ones(4))
        await memories.add_memory(spark_db, BOB, "bob's", "facts", np.ones(4))
        assert await memories.reassign_user(spark_db, "default", ALICE) == 2
        assert len(await memories.get_all_memories(spark_db, ALICE)) == 2
        assert len(await memories.get_all_memories(spark_db, BOB)) == 1


class TestActionHelpers:
    async def test_tool_permissions_upsert_and_read(self, spark_db: Database):
        aid = await autonomous_actions.create_action(spark_db, "a", "p", "m", ALICE)
        await autonomous_actions.set_tool_permission(spark_db, aid, "web_search", "denied", ALICE)
        await autonomous_actions.set_tool_permission(spark_db, aid, "web_search", "allowed", ALICE)
        await autonomous_actions.set_tool_permission(spark_db, aid, "read_file", "denied", ALICE)
        assert await autonomous_actions.get_tool_permissions(spark_db, aid) == {
            "web_search": "allowed",
            "read_file": "denied",
        }

    async def test_get_all_actions_includes_disabled_ones(self, spark_db: Database):
        a = await autonomous_actions.create_action(spark_db, "b-action", "p", "m", ALICE)
        await autonomous_actions.create_action(spark_db, "a-action", "p", "m", ALICE)
        await autonomous_actions.update_action(spark_db, a, ALICE, is_enabled=0)
        names = [r["name"] for r in await autonomous_actions.get_all_actions(spark_db)]
        assert names == ["a-action", "b-action"]

    async def test_clear_stale_locks_releases_only_old_locks(self, spark_db: Database):
        old = await autonomous_actions.create_action(spark_db, "old", "p", "m", ALICE)
        fresh = await autonomous_actions.create_action(spark_db, "fresh", "p", "m", ALICE)
        await autonomous_actions.update_action(
            spark_db, old, ALICE, locked_by="d1", locked_at="2026-10-29T08:00:00+00:00"
        )
        await autonomous_actions.update_action(
            spark_db, fresh, ALICE, locked_by="d2", locked_at="2026-10-29T09:50:00+00:00"
        )
        cleared = await autonomous_actions.clear_stale_locks(spark_db, "2026-10-29T09:30:00+00:00")
        assert [r["name"] for r in cleared] == ["old"]
        assert (await autonomous_actions.get_action(spark_db, old, ALICE))["locked_by"] is None
        assert (await autonomous_actions.get_action(spark_db, fresh, ALICE))["locked_by"] == "d2"

    async def test_action_stats(self, spark_db: Database):
        assert await autonomous_actions.action_stats(spark_db) == {
            "total": 0,
            "last_run": None,
            "next_run": None,
        }
        aid = await autonomous_actions.create_action(spark_db, "a", "p", "m", ALICE)
        await autonomous_actions.update_action(
            spark_db, aid, ALICE, next_run_at="2026-11-01T00:00:00+00:00"
        )
        run = await autonomous_actions.start_run(spark_db, aid, ALICE)
        await autonomous_actions.complete_run(spark_db, run)
        stats = await autonomous_actions.action_stats(spark_db)
        assert stats["total"] == 1
        assert stats["next_run"] == "2026-11-01T00:00:00+00:00"
        assert stats["last_run"] is not None


class TestConversationType:
    async def test_conversation_type(self, spark_db: Database):
        cid = await conversations.create_conversation(spark_db, "c", "m", ALICE)
        assert await conversations.get_conversation_type(spark_db, cid) == "standard"
        await conversations.update_conversation(spark_db, cid, ALICE, conversation_type="panel")
        assert await conversations.get_conversation_type(spark_db, cid) == "panel"
        assert await conversations.get_conversation_type(spark_db, 99999) is None


class TestViolations:
    async def test_record_violation(self, spark_db: Database):
        from spark.database import violations

        await violations.record_violation(
            spark_db,
            ALICE,
            violation_type="injection",
            severity="high",
            prompt_snippet="ignore previous instructions",
            detection_method="standard",
            action_taken="warn",
            confidence_score=1.0,
        )
        rows = await spark_db.fetch_all(
            "SELECT user_guid, violation_type, action_taken FROM prompt_inspection_violations"
        )
        assert rows == [{"user_guid": ALICE, "violation_type": "injection", "action_taken": "warn"}]
