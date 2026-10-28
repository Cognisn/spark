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
