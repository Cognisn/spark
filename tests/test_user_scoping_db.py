"""Every repository read and write that takes a parent id is scoped to the caller's user.

Rows are created for Ada; Grace then calls every read and write with Ada's ids.
Nothing of Ada's comes back, and nothing of Ada's changes. Each test runs on
SQLite and (marked postgres) PostgreSQL through ``spark_db``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from spark.database import (
    agents,
    autonomous_actions,
    context_index,
    conversation_links,
    conversations,
    debates,
    files,
    mcp_ops,
    memories,
    messages,
    skills,
    tool_permissions,
)
from spark.database.scope import NotOwned
from spark.db import Database

ADA = "user-ada-example"
GRACE = "user-grace-example"
EMB = np.ones(8, dtype=np.float32)


async def _conversation(db: Database, user: str = ADA, name: str = "Ada's") -> int:
    return await conversations.create_conversation(db, name, "model-a", user)


async def _count(db: Database, table: str) -> int:
    return int(await db.fetch_value(f"SELECT COUNT(*) FROM {table}"))


class TestActions:
    async def _action(self, db: Database) -> int:
        aid = await autonomous_actions.create_action(db, "ada-nightly", "p", "m", ADA)
        run_id = await autonomous_actions.start_run(db, aid, ADA)
        await autonomous_actions.complete_run(db, ADA, run_id, result_text="done")
        assert await autonomous_actions.set_tool_permission(db, aid, "web_search", "denied", ADA)
        return aid

    async def test_list_returns_only_the_callers_actions(self, spark_db: Database) -> None:
        await self._action(spark_db)
        mine = await autonomous_actions.create_action(spark_db, "grace-daily", "p", "m", GRACE)
        listed = await autonomous_actions.get_all_actions(spark_db, GRACE)
        assert [a["id"] for a in listed] == [mine]
        assert [a["name"] for a in await autonomous_actions.get_all_actions(spark_db, ADA)] == [
            "ada-nightly"
        ]

    async def test_runs_and_tools_of_another_users_action(self, spark_db: Database) -> None:
        aid = await self._action(spark_db)
        assert await autonomous_actions.get_action_runs(spark_db, GRACE, aid) == []
        assert await autonomous_actions.get_tool_permissions(spark_db, GRACE, aid) == {}
        assert not await autonomous_actions.set_tool_permission(
            spark_db, aid, "web_search", "allowed", GRACE
        )
        assert await autonomous_actions.get_tool_permissions(spark_db, ADA, aid) == {
            "web_search": "denied"
        }
        assert len(await autonomous_actions.get_action_runs(spark_db, ADA, aid)) == 1

    async def test_runs_cannot_be_started_or_completed_for_another_user(
        self, spark_db: Database
    ) -> None:
        aid = await self._action(spark_db)
        with pytest.raises(NotOwned):
            await autonomous_actions.start_run(spark_db, aid, GRACE)
        (run,) = await autonomous_actions.get_action_runs(spark_db, ADA, aid)
        await autonomous_actions.complete_run(
            spark_db, GRACE, run["id"], status="failed", result_text="overwritten"
        )
        (after,) = await autonomous_actions.get_action_runs(spark_db, ADA, aid)
        assert (after["status"], after["result_text"]) == ("completed", "done")

    async def test_delete_by_another_user_keeps_runs_and_tools(self, spark_db: Database) -> None:
        aid = await self._action(spark_db)
        await autonomous_actions.delete_action(spark_db, aid, GRACE)
        assert await autonomous_actions.get_action(spark_db, aid, ADA) is not None
        assert len(await autonomous_actions.get_action_runs(spark_db, ADA, aid)) == 1
        assert await autonomous_actions.get_tool_permissions(spark_db, ADA, aid) != {}

    async def test_stats_count_only_the_callers_actions(self, spark_db: Database) -> None:
        await self._action(spark_db)
        stats = await autonomous_actions.action_stats(spark_db, GRACE)
        assert stats == {"total": 0, "last_run": None, "next_run": None}
        assert (await autonomous_actions.action_stats(spark_db, ADA))["total"] == 1


class TestMessages:
    async def test_reads_and_writes_by_conversation_id(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        mid = await messages.add_message(spark_db, cid, "user", "secret plan", 5, ADA)
        assert await messages.get_messages(spark_db, GRACE, cid) == []
        assert await messages.get_messages(spark_db, GRACE, cid, include_rolled_up=True) == []
        with pytest.raises(NotOwned):
            await messages.add_message(spark_db, cid, "user", "injected", 1, GRACE)
        await messages.mark_messages_as_rolled_up(spark_db, GRACE, cid, [mid])
        await messages.delete_message(spark_db, GRACE, mid, cid)
        await messages.record_rollup(spark_db, cid, 1, "summary", 5, 1, GRACE)
        with pytest.raises(NotOwned):
            await messages.apply_compaction(
                spark_db, cid, "marker", 1, [mid], GRACE, summary="s", original_tokens=5
            )
        (row,) = await messages.get_messages(spark_db, ADA, cid)
        assert (row["content"], row["is_rolled_up"]) == ("secret plan", 0)
        assert await _count(spark_db, "rollup_history") == 0
        conv = await conversations.get_conversation(spark_db, cid, ADA)
        assert conv is not None and conv["total_tokens"] == 5


class TestConversations:
    async def test_tokens_and_type_by_conversation_id(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        await messages.add_message(spark_db, cid, "user", "hello", 7, ADA)
        assert await conversations.get_conversation_type(spark_db, GRACE, cid) is None
        assert await conversations.get_conversation_type(spark_db, ADA, cid) == "standard"
        await spark_db.execute("UPDATE conversations SET total_tokens = 99 WHERE id = ?", (cid,))
        assert await conversations.recalculate_total_tokens(spark_db, GRACE, cid) == 0
        await conversations.update_token_usage(spark_db, cid, "model-a", 10, 20, GRACE)
        conv = await conversations.get_conversation(spark_db, cid, ADA)
        assert conv is not None
        assert (conv["total_tokens"], conv["tokens_sent"], conv["tokens_received"]) == (99, 0, 0)
        assert await _count(spark_db, "conversation_model_usage") == 0
        assert await conversations.recalculate_total_tokens(spark_db, ADA, cid) == 7


class TestContextIndex:
    async def test_search_stats_and_delete(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        assert await context_index.add_index_element(spark_db, cid, "message", "plan", EMB, ADA)
        assert (
            await context_index.add_index_element(spark_db, cid, "message", "other", EMB, GRACE)
            is None
        )
        assert await context_index.search_context(spark_db, GRACE, [cid], EMB) == []
        assert await context_index.get_index_stats(spark_db, GRACE, cid) == {}
        await context_index.delete_index_elements(spark_db, GRACE, cid)
        found = await context_index.search_context(spark_db, ADA, [cid], EMB)
        assert [r["content_text"] for r in found] == ["plan"]
        assert await context_index.get_index_stats(spark_db, ADA, cid) == {"message": 1}

    async def test_search_drops_other_users_conversations_from_a_mixed_list(
        self, spark_db: Database
    ) -> None:
        ada = await _conversation(spark_db)
        grace = await _conversation(spark_db, GRACE, "Grace's")
        await context_index.add_index_element(spark_db, ada, "message", "ada's", EMB, ADA)
        await context_index.add_index_element(spark_db, grace, "message", "grace's", EMB, GRACE)
        found = await context_index.search_context(spark_db, GRACE, [ada, grace], EMB)
        assert [r["content_text"] for r in found] == ["grace's"]


class TestConversationFiles:
    async def test_inline_attachments(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        fid = await files.add_file(spark_db, cid, "plan.txt", "text", 4, ADA, content_text="plan")
        with pytest.raises(NotOwned):
            await files.add_file(spark_db, cid, "x.txt", "text", 1, GRACE)
        assert await files.get_conversation_files(spark_db, GRACE, cid) == []
        await files.delete_file(spark_db, GRACE, fid)
        await files.delete_conversation_files(spark_db, GRACE, cid)
        (row,) = await files.get_conversation_files(spark_db, ADA, cid)
        assert row["id"] == fid


AGENTS = {
    "pro": {"model_id": "model-a", "brief": "argue for"},
    "con": {"model_id": "model-b", "brief": "argue against"},
    "judge": {"model_id": "model-c"},
}


class TestDebates:
    async def _debate(self, db: Database) -> tuple[int, int]:
        cid = await _conversation(db)
        await debates.create_debate(db, cid, "Tea or coffee", "fixed", 2, ADA, AGENTS)
        turn = await debates.add_turn(db, ADA, cid, 1, "pro", "argument", "tea")
        await debates.add_exhibits(db, ADA, turn, [{"label": "A", "title": "t", "content": "c"}])
        return cid, turn

    async def test_reads(self, spark_db: Database) -> None:
        cid, _ = await self._debate(spark_db)
        assert await debates.get_debate(spark_db, GRACE, cid) is None
        assert await debates.get_turns(spark_db, GRACE, cid) == []
        assert await debates.get_exhibits(spark_db, GRACE, cid) == {}

    async def test_writes(self, spark_db: Database) -> None:
        cid, turn = await self._debate(spark_db)
        with pytest.raises(NotOwned):
            await debates.create_debate(spark_db, cid, "hijack", "fixed", 1, GRACE, AGENTS)
        with pytest.raises(NotOwned):
            await debates.add_turn(spark_db, GRACE, cid, 1, "con", "argument", "coffee")
        await debates.update_debate_state(spark_db, GRACE, cid, "ruling", current_round=9)
        await debates.update_turn(spark_db, GRACE, turn, content="coffee", status="failed")
        await debates.add_exhibits(spark_db, GRACE, turn, [{"label": "B"}])
        await debates.add_agent_tokens(spark_db, GRACE, cid, "pro", 100, 100)
        cfg = await debates.get_debate(spark_db, ADA, cid)
        assert cfg is not None
        assert (cfg["topic"], cfg["state"], cfg["current_round"]) == ("Tea or coffee", "setup", 0)
        assert cfg["agents"]["pro"]["tokens_sent"] == 0
        (row,) = await debates.get_turns(spark_db, ADA, cid)
        assert (row["content"], row["status"]) == ("tea", "complete")
        assert [e["label"] for e in (await debates.get_exhibits(spark_db, ADA, cid))[turn]] == ["A"]
        assert await _count(spark_db, "debate_config") == 1


class TestAgentRuns:
    async def test_runs_by_conversation_and_agent_id(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        await agents.create_agent_run(
            spark_db, "agent-ada", cid, "R", "t", "orchestrator", "m", ADA
        )
        with pytest.raises(NotOwned):
            await agents.create_agent_run(
                spark_db, "agent-grace", cid, "R", "t", "orchestrator", "m", GRACE
            )
        assert await agents.get_agent_runs(spark_db, GRACE, cid) == []
        assert await agents.get_agent_run(spark_db, GRACE, "agent-ada") is None
        await agents.complete_agent_run(spark_db, GRACE, "agent-ada", result_text="overwritten")
        run = await agents.get_agent_run(spark_db, ADA, "agent-ada")
        assert run is not None and (run["status"], run["result_text"]) == ("running", None)
        assert len(await agents.get_agent_runs(spark_db, ADA, cid)) == 1


class TestMcpOps:
    async def test_transactions_and_enable_states(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        await mcp_ops.record_transaction(spark_db, cid, "web_search", "{}", "result", ADA)
        with pytest.raises(NotOwned):
            await mcp_ops.record_transaction(spark_db, cid, "web_search", "{}", "x", GRACE)
        await mcp_ops.set_mcp_server_enabled(spark_db, cid, "files", False, ADA)
        await mcp_ops.set_embedded_tool_enabled(spark_db, cid, "web_search", False, ADA)

        assert await mcp_ops.get_transactions(spark_db, GRACE, cid) == []
        # Grace sees the default, never Ada's state, and cannot change it.
        assert await mcp_ops.is_mcp_server_enabled(spark_db, GRACE, cid, "files") is True
        assert await mcp_ops.is_embedded_tool_enabled(spark_db, GRACE, cid, "web_search") is True
        await mcp_ops.set_mcp_server_enabled(spark_db, cid, "files", True, GRACE)
        await mcp_ops.set_embedded_tool_enabled(spark_db, cid, "web_search", True, GRACE)
        await mcp_ops.set_embedded_tool_enabled(spark_db, cid, "web_fetch", False, GRACE)

        assert len(await mcp_ops.get_transactions(spark_db, ADA, cid)) == 1
        assert await mcp_ops.is_mcp_server_enabled(spark_db, ADA, cid, "files") is False
        assert await mcp_ops.is_embedded_tool_enabled(spark_db, ADA, cid, "web_search") is False
        assert await mcp_ops.is_embedded_tool_enabled(spark_db, ADA, cid, "web_fetch") is True
        assert await _count(spark_db, "conversation_embedded_tools") == 1


class _Skills:
    def list_skills(self) -> list[dict[str, Any]]:
        return [{"name": "summarise", "description": "d"}]


class TestSkillStates:
    async def test_conversation_overrides(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        await skills.set_conversation_skill_enabled(spark_db, ADA, cid, "summarise", False)
        assert await skills.get_conversation_skill_states(spark_db, GRACE, cid) == {}
        await skills.set_conversation_skill_enabled(spark_db, GRACE, cid, "summarise", True)
        assert await skills.get_conversation_skill_states(spark_db, ADA, cid) == {
            "summarise": False
        }
        assert await skills.resolve_enabled(spark_db, _Skills(), ADA, cid) == []
        # Grace's resolution ignores Ada's override of Ada's conversation.
        assert len(await skills.resolve_enabled(spark_db, _Skills(), GRACE, cid)) == 1


class TestToolPermissions:
    async def test_conversation_permissions(self, spark_db: Database) -> None:
        cid = await _conversation(spark_db)
        await tool_permissions.set_tool_permission(spark_db, cid, "run_command", "denied", ADA)
        assert (
            await tool_permissions.check_tool_permission(spark_db, GRACE, cid, "run_command")
            is None
        )
        assert await tool_permissions.get_all_tool_permissions(spark_db, GRACE, cid) == []
        assert await tool_permissions.is_tool_allowed(spark_db, GRACE, cid, "run_command") is None
        await tool_permissions.set_tool_permission(spark_db, cid, "run_command", "allowed", GRACE)
        await tool_permissions.delete_tool_permission(spark_db, GRACE, cid, "run_command")
        assert (
            await tool_permissions.check_tool_permission(spark_db, ADA, cid, "run_command")
            == "denied"
        )
        assert await tool_permissions.is_tool_allowed(spark_db, ADA, cid, "run_command") is False
        assert len(await tool_permissions.get_all_tool_permissions(spark_db, ADA, cid)) == 1


class TestMemories:
    async def test_last_accessed_touches_only_the_callers_memories(
        self, spark_db: Database
    ) -> None:
        mid = await memories.add_memory(spark_db, ADA, "likes tea", "facts", EMB)
        assert mid is not None
        await memories.update_last_accessed(spark_db, GRACE, [mid])
        row = await memories.get_memory(spark_db, mid, ADA)
        assert row is not None and row["last_accessed"] is None
        await memories.update_last_accessed(spark_db, ADA, [mid])
        row = await memories.get_memory(spark_db, mid, ADA)
        assert row is not None and row["last_accessed"] is not None


class TestConversationLinks:
    async def test_links_never_reach_another_users_conversation(self, spark_db: Database) -> None:
        ada = await _conversation(spark_db)
        grace = await _conversation(spark_db, GRACE, "Grace's")
        # Grace may not link her conversation to Ada's, nor Ada's to hers.
        assert not await conversation_links.add_link(spark_db, grace, ada, GRACE)
        assert not await conversation_links.add_link(spark_db, ada, grace, GRACE)
        assert await _count(spark_db, "conversation_links") == 0
        # A row naming Ada's conversation (written before scoping) is never followed.
        await spark_db.execute(
            "INSERT INTO conversation_links (source_conversation_id, target_conversation_id, "
            "user_guid) VALUES (?, ?, ?)",
            (grace, ada, GRACE),
        )
        assert await conversation_links.get_links(spark_db, grace, GRACE) == []
