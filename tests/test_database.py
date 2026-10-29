"""Tests for the database layer."""

from __future__ import annotations

import numpy as np
import pytest

from spark.database import (
    autonomous_actions,
    context_index,
    conversations,
    files,
    mcp_ops,
    memories,
    messages,
    tool_permissions,
    usage,
)
from spark.db import Database

USER = "test-user-guid"


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


@pytest.fixture()
def conn(spark_db):
    return spark_db


# -- Conversations ------------------------------------------------------------


class TestConversations:
    async def test_create_and_get(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Test Chat", "claude-3", USER)
        assert cid > 0
        conv = await conversations.get_conversation(conn, cid, USER)
        assert conv is not None
        assert conv["name"] == "Test Chat"
        assert conv["model_id"] == "claude-3"

    async def test_get_active(self, conn: Database) -> None:
        await conversations.create_conversation(conn, "Chat 1", "m1", USER)
        await conversations.create_conversation(conn, "Chat 2", "m2", USER)
        active = await conversations.get_active_conversations(conn, USER)
        assert len(active) == 2

    async def test_delete_soft(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "To Delete", "m1", USER)
        await conversations.delete_conversation(conn, cid, USER)
        conv = await conversations.get_conversation(conn, cid, USER)
        assert conv is not None
        assert conv["is_active"] == 0
        active = await conversations.get_active_conversations(conn, USER)
        assert len(active) == 0

    async def test_update(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Old Name", "m1", USER)
        await conversations.update_conversation(conn, cid, USER, name="New Name")
        conv = await conversations.get_conversation(conn, cid, USER)
        assert conv["name"] == "New Name"

    async def test_search(self, conn: Database) -> None:
        await conversations.create_conversation(conn, "Python Help", "m1", USER)
        await conversations.create_conversation(conn, "Rust Help", "m1", USER)
        results = await conversations.search_conversations(conn, "Python", USER)
        assert len(results) == 1
        assert results[0]["name"] == "Python Help"

    async def test_token_usage(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await conversations.update_token_usage(conn, cid, "m1", 100, 50, USER)
        conv = await conversations.get_conversation(conn, cid, USER)
        assert conv["tokens_sent"] == 100
        assert conv["tokens_received"] == 50
        # total_tokens tracks active context, not lifetime billing — no messages added yet
        assert conv["total_tokens"] == 0

    async def test_total_tokens_tracks_active_messages(self, conn: Database) -> None:
        """total_tokens is maintained by message inserts, not token-usage updates."""
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await messages.add_message(conn, cid, "user", "hello", 10, USER)
        await messages.add_message(conn, cid, "assistant", "hi", 5, USER)
        # Billing update must not inflate total_tokens
        await conversations.update_token_usage(conn, cid, "m1", 1000, 500, USER)
        conv = await conversations.get_conversation(conn, cid, USER)
        assert conv["total_tokens"] == 15
        assert conv["tokens_sent"] == 1000
        assert conv["tokens_received"] == 500

    async def test_recalculate_tokens(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await messages.add_message(conn, cid, "user", "hello", 10, USER)
        await messages.add_message(conn, cid, "assistant", "hi", 5, USER)
        total = await conversations.recalculate_total_tokens(conn, cid)
        assert total == 15


# -- Messages -----------------------------------------------------------------


class TestMessages:
    async def test_add_and_get(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        mid = await messages.add_message(conn, cid, "user", "Hello!", 10, USER)
        assert mid > 0
        msgs = await messages.get_messages(conn, cid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello!"

    async def test_rollup(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        m1 = await messages.add_message(conn, cid, "user", "msg1", 10, USER)
        m2 = await messages.add_message(conn, cid, "assistant", "msg2", 10, USER)
        await messages.mark_messages_as_rolled_up(conn, cid, [m1, m2])

        active = await messages.get_messages(conn, cid)
        assert len(active) == 0

        all_msgs = await messages.get_messages(conn, cid, include_rolled_up=True)
        assert len(all_msgs) == 2

    async def test_record_rollup(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await messages.record_rollup(conn, cid, 5, "summary", 500, 100, USER)
        # Should not raise

    async def test_delete_message(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        mid = await messages.add_message(conn, cid, "user", "to delete", 20, USER)
        await messages.delete_message(conn, mid, cid)
        msgs = await messages.get_messages(conn, cid)
        assert len(msgs) == 0


# -- Files --------------------------------------------------------------------


class TestFiles:
    async def test_add_and_get(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        fid = await files.add_file(conn, cid, "test.txt", "text", 100, USER, content_text="hello")
        assert fid > 0
        result = await files.get_conversation_files(conn, cid)
        assert len(result) == 1
        assert result[0]["filename"] == "test.txt"

    async def test_delete_file(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        fid = await files.add_file(conn, cid, "test.txt", "text", 100, USER)
        await files.delete_file(conn, fid)
        assert len(await files.get_conversation_files(conn, cid)) == 0

    async def test_delete_conversation_files(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await files.add_file(conn, cid, "a.txt", "text", 50, USER)
        await files.add_file(conn, cid, "b.txt", "text", 50, USER)
        await files.delete_conversation_files(conn, cid)
        assert len(await files.get_conversation_files(conn, cid)) == 0


# -- Memories -----------------------------------------------------------------


class TestMemories:
    def _emb(self, dim: int = 128) -> np.ndarray:
        return np.random.randn(dim).astype(np.float32)

    async def test_add_and_get(self, conn: Database) -> None:
        emb = self._emb()
        mid = await memories.add_memory(conn, USER, "test fact", "facts", emb)
        assert mid is not None
        mem = await memories.get_memory(conn, mid, USER)
        assert mem is not None
        assert mem["content"] == "test fact"
        assert isinstance(mem["embedding"], np.ndarray)

    async def test_duplicate_blocked(self, conn: Database) -> None:
        emb = self._emb()
        await memories.add_memory(conn, USER, "same content", "facts", emb)
        result = await memories.add_memory(conn, USER, "same content", "facts", emb)
        assert result is None

    async def test_search(self, conn: Database) -> None:
        emb1 = self._emb()
        emb2 = emb1 + np.random.randn(128).astype(np.float32) * 0.01  # similar
        await memories.add_memory(conn, USER, "python is great", "facts", emb1)
        results = await memories.search_memories(conn, USER, emb2, threshold=0.0)
        assert len(results) >= 1

    async def test_delete(self, conn: Database) -> None:
        mid = await memories.add_memory(conn, USER, "to delete", "facts", self._emb())
        await memories.delete_memory(conn, mid, USER)
        assert await memories.get_memory(conn, mid, USER) is None

    async def test_exists(self, conn: Database) -> None:
        await memories.add_memory(conn, USER, "exists check", "facts", self._emb())
        assert await memories.memory_exists(conn, USER, "exists check") is True
        assert await memories.memory_exists(conn, USER, "nope") is False

    async def test_get_all(self, conn: Database) -> None:
        await memories.add_memory(conn, USER, "fact 1", "facts", self._emb())
        await memories.add_memory(conn, USER, "fact 2", "facts", self._emb())
        all_mems = await memories.get_all_memories(conn, USER)
        assert len(all_mems) == 2

    async def test_delete_all(self, conn: Database) -> None:
        await memories.add_memory(conn, USER, "mem a", "facts", self._emb())
        await memories.add_memory(conn, USER, "mem b", "facts", self._emb())
        await memories.delete_all_memories(conn, USER)
        assert len(await memories.get_all_memories(conn, USER)) == 0


# -- Tool Permissions ---------------------------------------------------------


class TestToolPermissions:
    async def test_no_permission(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        assert await tool_permissions.check_tool_permission(conn, cid, "read_file") is None
        assert await tool_permissions.is_tool_allowed(conn, cid, "read_file") is None

    async def test_set_allowed(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await tool_permissions.set_tool_permission(conn, cid, "read_file", "allowed", USER)
        assert await tool_permissions.is_tool_allowed(conn, cid, "read_file") is True

    async def test_set_denied(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await tool_permissions.set_tool_permission(conn, cid, "write_file", "denied", USER)
        assert await tool_permissions.is_tool_allowed(conn, cid, "write_file") is False

    async def test_get_all(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await tool_permissions.set_tool_permission(conn, cid, "tool_a", "allowed", USER)
        await tool_permissions.set_tool_permission(conn, cid, "tool_b", "denied", USER)
        perms = await tool_permissions.get_all_tool_permissions(conn, cid)
        assert len(perms) == 2

    async def test_delete_permission(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await tool_permissions.set_tool_permission(conn, cid, "tool_x", "allowed", USER)
        await tool_permissions.delete_tool_permission(conn, cid, "tool_x")
        assert await tool_permissions.check_tool_permission(conn, cid, "tool_x") is None


# -- Usage Tracking -----------------------------------------------------------


class TestUsage:
    async def test_record_and_query(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await usage.record_usage(conn, cid, "claude-3", 1000, 500, USER, cost=0.05)
        result = await usage.get_token_usage_in_window(conn, USER, "2020-01-01")
        assert result["input_tokens"] == 1000
        assert result["output_tokens"] == 500

    async def test_summary(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await usage.record_usage(conn, cid, "claude-3", 1000, 500, USER)
        await usage.record_usage(conn, cid, "claude-3", 2000, 1000, USER)
        summary = await usage.get_usage_summary(conn, USER, "2020-01-01")
        assert len(summary) == 1
        assert summary[0]["request_count"] == 2

    async def test_cleanup(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await usage.record_usage(conn, cid, "claude-3", 100, 50, USER)
        deleted = await usage.cleanup_old_usage(conn, "2099-01-01")
        assert deleted >= 1


# -- Context Index ------------------------------------------------------------


class TestContextIndex:
    def _emb(self, dim: int = 128) -> np.ndarray:
        return np.random.randn(dim).astype(np.float32)

    async def test_add_and_search(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        emb = self._emb()
        eid = await context_index.add_index_element(conn, cid, "user", "hello world", emb, USER)
        assert eid is not None

        query = emb + np.random.randn(128).astype(np.float32) * 0.01
        results = await context_index.search_context(conn, [cid], query, threshold=0.0)
        assert len(results) >= 1

    async def test_duplicate_blocked(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        emb = self._emb()
        await context_index.add_index_element(conn, cid, "user", "same text", emb, USER)
        result = await context_index.add_index_element(conn, cid, "user", "same text", emb, USER)
        assert result is None

    async def test_delete(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await context_index.add_index_element(conn, cid, "user", "test", self._emb(), USER)
        await context_index.delete_index_elements(conn, cid)
        stats = await context_index.get_index_stats(conn, cid)
        assert len(stats) == 0

    async def test_stats(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await context_index.add_index_element(conn, cid, "user", "msg1", self._emb(), USER)
        await context_index.add_index_element(conn, cid, "assistant", "msg2", self._emb(), USER)
        stats = await context_index.get_index_stats(conn, cid)
        assert stats.get("user") == 1
        assert stats.get("assistant") == 1


# -- MCP Operations -----------------------------------------------------------


class TestMCPOps:
    async def test_record_transaction(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        tid = await mcp_ops.record_transaction(
            conn, cid, "read_file", '{"path": "/tmp"}', "contents", USER
        )
        assert tid > 0

    async def test_get_transactions(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await mcp_ops.record_transaction(conn, cid, "tool_a", "{}", "ok", USER)
        await mcp_ops.record_transaction(conn, cid, "tool_b", "{}", "ok", USER)
        txs = await mcp_ops.get_transactions(conn, cid)
        assert len(txs) == 2

    async def test_filter_by_tool(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        await mcp_ops.record_transaction(conn, cid, "read_file", "{}", "ok", USER)
        await mcp_ops.record_transaction(conn, cid, "write_file", "{}", "ok", USER)
        txs = await mcp_ops.get_transactions(conn, cid, tool_name="read_file")
        assert len(txs) == 1

    async def test_mcp_server_enabled(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        assert await mcp_ops.is_mcp_server_enabled(conn, cid, "my-server") is True
        await mcp_ops.set_mcp_server_enabled(conn, cid, "my-server", False, USER)
        assert await mcp_ops.is_mcp_server_enabled(conn, cid, "my-server") is False

    async def test_embedded_tool_enabled(self, conn: Database) -> None:
        cid = await conversations.create_conversation(conn, "Chat", "m1", USER)
        assert await mcp_ops.is_embedded_tool_enabled(conn, cid, "filesystem") is True
        await mcp_ops.set_embedded_tool_enabled(conn, cid, "filesystem", False, USER)
        assert await mcp_ops.is_embedded_tool_enabled(conn, cid, "filesystem") is False


# -- Autonomous Actions -------------------------------------------------------


class TestAutonomousActions:
    async def test_create_and_get(self, conn: Database) -> None:
        aid = await autonomous_actions.create_action(
            conn, "Daily Report", "Generate report", "claude-3", USER
        )
        assert aid > 0
        action = await autonomous_actions.get_action(conn, aid, USER)
        assert action is not None
        assert action["name"] == "Daily Report"

    async def test_get_enabled(self, conn: Database) -> None:
        await autonomous_actions.create_action(conn, "Action 1", "prompt 1", "m1", USER)
        await autonomous_actions.create_action(conn, "Action 2", "prompt 2", "m1", USER)
        enabled = await autonomous_actions.get_enabled_actions(conn, USER)
        assert len(enabled) == 2

    async def test_update(self, conn: Database) -> None:
        aid = await autonomous_actions.create_action(conn, "Test", "prompt", "m1", USER)
        await autonomous_actions.update_action(conn, aid, USER, description="Updated")
        action = await autonomous_actions.get_action(conn, aid, USER)
        assert action["description"] == "Updated"

    async def test_delete(self, conn: Database) -> None:
        aid = await autonomous_actions.create_action(conn, "To Delete", "prompt", "m1", USER)
        await autonomous_actions.delete_action(conn, aid, USER)
        assert await autonomous_actions.get_action(conn, aid, USER) is None

    async def test_run_lifecycle(self, conn: Database) -> None:
        aid = await autonomous_actions.create_action(conn, "Runner", "prompt", "m1", USER)
        rid = await autonomous_actions.start_run(conn, aid, USER)
        assert rid > 0
        await autonomous_actions.complete_run(conn, rid, result_text="Done", input_tokens=100)
        runs = await autonomous_actions.get_action_runs(conn, aid)
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"

    async def test_locking(self, conn: Database) -> None:
        aid = await autonomous_actions.create_action(conn, "Lockable", "prompt", "m1", USER)
        assert await autonomous_actions.try_lock_action(conn, aid, "daemon-1") is True
        assert await autonomous_actions.try_lock_action(conn, aid, "daemon-2") is False
        await autonomous_actions.unlock_action(conn, aid)
        assert await autonomous_actions.try_lock_action(conn, aid, "daemon-2") is True


# -- Database Facade ----------------------------------------------------------
