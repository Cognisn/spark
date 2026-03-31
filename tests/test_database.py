"""Tests for the database layer."""

from __future__ import annotations

import numpy as np
import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.database import conversations, messages, files, memories
from spark.database import tool_permissions, usage, context_index, mcp_ops
from spark.database import autonomous_actions


USER = "test-user-guid"


@pytest.fixture()
def db(tmp_path) -> Database:
    """Create an in-memory test database."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    return Database(conn)


@pytest.fixture()
def conn(db: Database) -> DatabaseConnection:
    return db.connection


# -- Backend ------------------------------------------------------------------


class TestSQLiteBackend:
    def test_placeholder(self) -> None:
        backend = SQLiteBackend(":memory:")
        assert backend.placeholder() == "?"

    def test_autoincrement(self) -> None:
        backend = SQLiteBackend(":memory:")
        assert "AUTOINCREMENT" in backend.autoincrement()

    def test_upsert_sql(self) -> None:
        backend = SQLiteBackend(":memory:")
        sql = backend.upsert_sql("t", ["a", "b", "c"], ["a"])
        assert "ON CONFLICT(a)" in sql
        assert "excluded.b" in sql
        assert "excluded.c" in sql


# -- Conversations ------------------------------------------------------------


class TestConversations:
    def test_create_and_get(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Test Chat", "claude-3", USER)
        assert cid > 0
        conv = conversations.get_conversation(conn, cid, USER)
        assert conv is not None
        assert conv["name"] == "Test Chat"
        assert conv["model_id"] == "claude-3"

    def test_get_active(self, conn: DatabaseConnection) -> None:
        conversations.create_conversation(conn, "Chat 1", "m1", USER)
        conversations.create_conversation(conn, "Chat 2", "m2", USER)
        active = conversations.get_active_conversations(conn, USER)
        assert len(active) == 2

    def test_delete_soft(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "To Delete", "m1", USER)
        conversations.delete_conversation(conn, cid, USER)
        conv = conversations.get_conversation(conn, cid, USER)
        assert conv is not None
        assert conv["is_active"] == 0
        active = conversations.get_active_conversations(conn, USER)
        assert len(active) == 0

    def test_update(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Old Name", "m1", USER)
        conversations.update_conversation(conn, cid, USER, name="New Name")
        conv = conversations.get_conversation(conn, cid, USER)
        assert conv["name"] == "New Name"

    def test_search(self, conn: DatabaseConnection) -> None:
        conversations.create_conversation(conn, "Python Help", "m1", USER)
        conversations.create_conversation(conn, "Rust Help", "m1", USER)
        results = conversations.search_conversations(conn, "Python", USER)
        assert len(results) == 1
        assert results[0]["name"] == "Python Help"

    def test_token_usage(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        conversations.update_token_usage(conn, cid, "m1", 100, 50, USER)
        conv = conversations.get_conversation(conn, cid, USER)
        assert conv["tokens_sent"] == 100
        assert conv["tokens_received"] == 50
        assert conv["total_tokens"] == 150

    def test_recalculate_tokens(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        messages.add_message(conn, cid, "user", "hello", 10, USER)
        messages.add_message(conn, cid, "assistant", "hi", 5, USER)
        total = conversations.recalculate_total_tokens(conn, cid)
        assert total == 15


# -- Messages -----------------------------------------------------------------


class TestMessages:
    def test_add_and_get(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        mid = messages.add_message(conn, cid, "user", "Hello!", 10, USER)
        assert mid > 0
        msgs = messages.get_messages(conn, cid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello!"

    def test_rollup(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        m1 = messages.add_message(conn, cid, "user", "msg1", 10, USER)
        m2 = messages.add_message(conn, cid, "assistant", "msg2", 10, USER)
        messages.mark_messages_as_rolled_up(conn, cid, [m1, m2])

        active = messages.get_messages(conn, cid)
        assert len(active) == 0

        all_msgs = messages.get_messages(conn, cid, include_rolled_up=True)
        assert len(all_msgs) == 2

    def test_record_rollup(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        messages.record_rollup(conn, cid, 5, "summary", 500, 100, USER)
        # Should not raise

    def test_delete_message(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        mid = messages.add_message(conn, cid, "user", "to delete", 20, USER)
        messages.delete_message(conn, mid, cid)
        msgs = messages.get_messages(conn, cid)
        assert len(msgs) == 0


# -- Files --------------------------------------------------------------------


class TestFiles:
    def test_add_and_get(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        fid = files.add_file(conn, cid, "test.txt", "text", 100, USER, content_text="hello")
        assert fid > 0
        result = files.get_conversation_files(conn, cid)
        assert len(result) == 1
        assert result[0]["filename"] == "test.txt"

    def test_delete_file(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        fid = files.add_file(conn, cid, "test.txt", "text", 100, USER)
        files.delete_file(conn, fid)
        assert len(files.get_conversation_files(conn, cid)) == 0

    def test_delete_conversation_files(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        files.add_file(conn, cid, "a.txt", "text", 50, USER)
        files.add_file(conn, cid, "b.txt", "text", 50, USER)
        files.delete_conversation_files(conn, cid)
        assert len(files.get_conversation_files(conn, cid)) == 0


# -- Memories -----------------------------------------------------------------


class TestMemories:
    def _emb(self, dim: int = 128) -> np.ndarray:
        return np.random.randn(dim).astype(np.float32)

    def test_add_and_get(self, conn: DatabaseConnection) -> None:
        emb = self._emb()
        mid = memories.add_memory(conn, USER, "test fact", "facts", emb)
        assert mid is not None
        mem = memories.get_memory(conn, mid, USER)
        assert mem is not None
        assert mem["content"] == "test fact"
        assert isinstance(mem["embedding"], np.ndarray)

    def test_duplicate_blocked(self, conn: DatabaseConnection) -> None:
        emb = self._emb()
        memories.add_memory(conn, USER, "same content", "facts", emb)
        result = memories.add_memory(conn, USER, "same content", "facts", emb)
        assert result is None

    def test_search(self, conn: DatabaseConnection) -> None:
        emb1 = self._emb()
        emb2 = emb1 + np.random.randn(128).astype(np.float32) * 0.01  # similar
        memories.add_memory(conn, USER, "python is great", "facts", emb1)
        results = memories.search_memories(conn, USER, emb2, threshold=0.0)
        assert len(results) >= 1

    def test_delete(self, conn: DatabaseConnection) -> None:
        mid = memories.add_memory(conn, USER, "to delete", "facts", self._emb())
        memories.delete_memory(conn, mid, USER)
        assert memories.get_memory(conn, mid, USER) is None

    def test_exists(self, conn: DatabaseConnection) -> None:
        memories.add_memory(conn, USER, "exists check", "facts", self._emb())
        assert memories.memory_exists(conn, USER, "exists check") is True
        assert memories.memory_exists(conn, USER, "nope") is False

    def test_get_all(self, conn: DatabaseConnection) -> None:
        memories.add_memory(conn, USER, "fact 1", "facts", self._emb())
        memories.add_memory(conn, USER, "fact 2", "facts", self._emb())
        all_mems = memories.get_all_memories(conn, USER)
        assert len(all_mems) == 2

    def test_delete_all(self, conn: DatabaseConnection) -> None:
        memories.add_memory(conn, USER, "mem a", "facts", self._emb())
        memories.add_memory(conn, USER, "mem b", "facts", self._emb())
        memories.delete_all_memories(conn, USER)
        assert len(memories.get_all_memories(conn, USER)) == 0


# -- Tool Permissions ---------------------------------------------------------


class TestToolPermissions:
    def test_no_permission(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        assert tool_permissions.check_tool_permission(conn, cid, "read_file") is None
        assert tool_permissions.is_tool_allowed(conn, cid, "read_file") is None

    def test_set_allowed(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        tool_permissions.set_tool_permission(conn, cid, "read_file", "allowed", USER)
        assert tool_permissions.is_tool_allowed(conn, cid, "read_file") is True

    def test_set_denied(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        tool_permissions.set_tool_permission(conn, cid, "write_file", "denied", USER)
        assert tool_permissions.is_tool_allowed(conn, cid, "write_file") is False

    def test_get_all(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        tool_permissions.set_tool_permission(conn, cid, "tool_a", "allowed", USER)
        tool_permissions.set_tool_permission(conn, cid, "tool_b", "denied", USER)
        perms = tool_permissions.get_all_tool_permissions(conn, cid)
        assert len(perms) == 2

    def test_delete_permission(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        tool_permissions.set_tool_permission(conn, cid, "tool_x", "allowed", USER)
        tool_permissions.delete_tool_permission(conn, cid, "tool_x")
        assert tool_permissions.check_tool_permission(conn, cid, "tool_x") is None


# -- Usage Tracking -----------------------------------------------------------


class TestUsage:
    def test_record_and_query(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        usage.record_usage(conn, cid, "claude-3", 1000, 500, USER, cost=0.05)
        result = usage.get_token_usage_in_window(conn, USER, "2020-01-01")
        assert result["input_tokens"] == 1000
        assert result["output_tokens"] == 500

    def test_summary(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        usage.record_usage(conn, cid, "claude-3", 1000, 500, USER)
        usage.record_usage(conn, cid, "claude-3", 2000, 1000, USER)
        summary = usage.get_usage_summary(conn, USER, "2020-01-01")
        assert len(summary) == 1
        assert summary[0]["request_count"] == 2

    def test_cleanup(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        usage.record_usage(conn, cid, "claude-3", 100, 50, USER)
        deleted = usage.cleanup_old_usage(conn, "2099-01-01")
        assert deleted >= 1


# -- Context Index ------------------------------------------------------------


class TestContextIndex:
    def _emb(self, dim: int = 128) -> np.ndarray:
        return np.random.randn(dim).astype(np.float32)

    def test_add_and_search(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        emb = self._emb()
        eid = context_index.add_index_element(conn, cid, "user", "hello world", emb, USER)
        assert eid is not None

        query = emb + np.random.randn(128).astype(np.float32) * 0.01
        results = context_index.search_context(conn, [cid], query, threshold=0.0)
        assert len(results) >= 1

    def test_duplicate_blocked(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        emb = self._emb()
        context_index.add_index_element(conn, cid, "user", "same text", emb, USER)
        result = context_index.add_index_element(conn, cid, "user", "same text", emb, USER)
        assert result is None

    def test_delete(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        context_index.add_index_element(conn, cid, "user", "test", self._emb(), USER)
        context_index.delete_index_elements(conn, cid)
        stats = context_index.get_index_stats(conn, cid)
        assert len(stats) == 0

    def test_stats(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        context_index.add_index_element(conn, cid, "user", "msg1", self._emb(), USER)
        context_index.add_index_element(conn, cid, "assistant", "msg2", self._emb(), USER)
        stats = context_index.get_index_stats(conn, cid)
        assert stats.get("user") == 1
        assert stats.get("assistant") == 1


# -- MCP Operations -----------------------------------------------------------


class TestMCPOps:
    def test_record_transaction(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        tid = mcp_ops.record_transaction(
            conn, cid, "read_file", '{"path": "/tmp"}', "contents", USER
        )
        assert tid > 0

    def test_get_transactions(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        mcp_ops.record_transaction(conn, cid, "tool_a", "{}", "ok", USER)
        mcp_ops.record_transaction(conn, cid, "tool_b", "{}", "ok", USER)
        txs = mcp_ops.get_transactions(conn, cid)
        assert len(txs) == 2

    def test_filter_by_tool(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        mcp_ops.record_transaction(conn, cid, "read_file", "{}", "ok", USER)
        mcp_ops.record_transaction(conn, cid, "write_file", "{}", "ok", USER)
        txs = mcp_ops.get_transactions(conn, cid, tool_name="read_file")
        assert len(txs) == 1

    def test_mcp_server_enabled(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        assert mcp_ops.is_mcp_server_enabled(conn, cid, "my-server") is True
        mcp_ops.set_mcp_server_enabled(conn, cid, "my-server", False, USER)
        assert mcp_ops.is_mcp_server_enabled(conn, cid, "my-server") is False

    def test_embedded_tool_enabled(self, conn: DatabaseConnection) -> None:
        cid = conversations.create_conversation(conn, "Chat", "m1", USER)
        assert mcp_ops.is_embedded_tool_enabled(conn, cid, "filesystem") is True
        mcp_ops.set_embedded_tool_enabled(conn, cid, "filesystem", False, USER)
        assert mcp_ops.is_embedded_tool_enabled(conn, cid, "filesystem") is False


# -- Autonomous Actions -------------------------------------------------------


class TestAutonomousActions:
    def test_create_and_get(self, conn: DatabaseConnection) -> None:
        aid = autonomous_actions.create_action(
            conn, "Daily Report", "Generate report", "claude-3", USER
        )
        assert aid > 0
        action = autonomous_actions.get_action(conn, aid, USER)
        assert action is not None
        assert action["name"] == "Daily Report"

    def test_get_enabled(self, conn: DatabaseConnection) -> None:
        autonomous_actions.create_action(conn, "Action 1", "prompt 1", "m1", USER)
        autonomous_actions.create_action(conn, "Action 2", "prompt 2", "m1", USER)
        enabled = autonomous_actions.get_enabled_actions(conn, USER)
        assert len(enabled) == 2

    def test_update(self, conn: DatabaseConnection) -> None:
        aid = autonomous_actions.create_action(conn, "Test", "prompt", "m1", USER)
        autonomous_actions.update_action(conn, aid, USER, description="Updated")
        action = autonomous_actions.get_action(conn, aid, USER)
        assert action["description"] == "Updated"

    def test_delete(self, conn: DatabaseConnection) -> None:
        aid = autonomous_actions.create_action(conn, "To Delete", "prompt", "m1", USER)
        autonomous_actions.delete_action(conn, aid, USER)
        assert autonomous_actions.get_action(conn, aid, USER) is None

    def test_run_lifecycle(self, conn: DatabaseConnection) -> None:
        aid = autonomous_actions.create_action(conn, "Runner", "prompt", "m1", USER)
        rid = autonomous_actions.start_run(conn, aid, USER)
        assert rid > 0
        autonomous_actions.complete_run(conn, rid, result_text="Done", input_tokens=100)
        runs = autonomous_actions.get_action_runs(conn, aid)
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"

    def test_locking(self, conn: DatabaseConnection) -> None:
        aid = autonomous_actions.create_action(conn, "Lockable", "prompt", "m1", USER)
        assert autonomous_actions.try_lock_action(conn, aid, "daemon-1") is True
        assert autonomous_actions.try_lock_action(conn, aid, "daemon-2") is False
        autonomous_actions.unlock_action(conn, aid)
        assert autonomous_actions.try_lock_action(conn, aid, "daemon-2") is True


# -- Database Facade ----------------------------------------------------------


class TestDatabaseFacade:
    def test_from_settings(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.get = lambda key, default=None, **kw: {
            "database.type": "sqlite",
            "database.path": str(tmp_path / "facade.db"),
        }.get(key, default)

        db = Database.from_settings(settings)
        assert db.connection is not None
        db.close()

    def test_module_properties(self, db: Database) -> None:
        from spark.database import conversations as conv_mod

        assert db.conversations is conv_mod
        assert db.messages is not None
        assert db.files is not None
        assert db.memories is not None
        assert db.tool_permissions is not None
        assert db.usage is not None
        assert db.context_index is not None
        assert db.mcp_ops is not None
        assert db.autonomous_actions is not None
