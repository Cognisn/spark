"""Conversation sharing in the repositories: the one access decision, membership, transfer
and the turn lock. Each test runs on SQLite and (marked postgres) PostgreSQL.

Ada owns a conversation; Cora is a contributor, Vic a viewer and Sam a stranger.
Every refusal here was mutation-checked: flipping the role table entry or the fragment
that decides it makes the test fail.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from spark.database import (
    agents,
    context_index,
    conversation_links,
    conversation_members,
    conversations,
    debates,
    files,
    mcp_ops,
    message_reasoning,
    messages,
    skills,
    tool_permissions,
)
from spark.database.scope import (
    Capability,
    NotOwned,
    NotPermitted,
    Role,
    allows,
    conversation_role,
    require_access,
)
from spark.db import Database

ADA = "user-ada-example"
CORA = "user-cora-example"
VIC = "user-vic-example"
SAM = "user-sam-example"
EMB = np.ones(8, dtype=np.float32)


async def _shared(db: Database) -> int:
    cid = await conversations.create_conversation(db, "Ada's plan", "model-a", ADA)
    await messages.add_message(db, cid, "user", "hello", 1, ADA)
    await conversation_members.share(db, ADA, cid, CORA, Role.CONTRIBUTOR)
    await conversation_members.share(db, ADA, cid, VIC, Role.VIEWER)
    return cid


async def _owner_in_step(db: Database, cid: int) -> str:
    """The invariant: exactly one owner row, naming conversations.user_guid."""
    owner = await db.fetch_value("SELECT user_guid FROM conversations WHERE id = ?", (cid,))
    rows = await db.fetch_all(
        "SELECT user_guid FROM conversation_members WHERE conversation_id = ? AND role = 'owner'",
        (cid,),
    )
    assert [r["user_guid"] for r in rows] == [owner]
    return str(owner)


class TestTheRoleTable:
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (Role.OWNER, {Capability.READ, Capability.CONTRIBUTE, Capability.MANAGE}),
            (Role.CONTRIBUTOR, {Capability.READ, Capability.CONTRIBUTE}),
            (Role.VIEWER, {Capability.READ}),
            (None, set()),
        ],
    )
    def test_each_role_allows_exactly(self, role: Role | None, allowed: set[Capability]) -> None:
        assert {c for c in Capability if allows(role, c)} == allowed

    async def test_roles_in_a_shared_conversation(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        assert await conversation_role(spark_db, ADA, cid) is Role.OWNER
        assert await conversation_role(spark_db, CORA, cid) is Role.CONTRIBUTOR
        assert await conversation_role(spark_db, VIC, cid) is Role.VIEWER
        assert await conversation_role(spark_db, SAM, cid) is None
        assert await conversation_role(spark_db, ADA, cid + 999) is None
        assert await conversation_role(spark_db, "", cid) is None

    async def test_require_access_refuses_404_or_403(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        assert await require_access(spark_db, VIC, cid, Capability.READ) is Role.VIEWER
        with pytest.raises(NotPermitted):
            await require_access(spark_db, VIC, cid, Capability.CONTRIBUTE)
        with pytest.raises(NotPermitted):
            await require_access(spark_db, CORA, cid, Capability.MANAGE)
        with pytest.raises(NotOwned) as stranger:
            await require_access(spark_db, SAM, cid, Capability.READ)
        assert not isinstance(stranger.value, NotPermitted)


class TestMembership:
    async def test_create_writes_the_owner_row(self, spark_db: Database) -> None:
        cid = await conversations.create_conversation(spark_db, "c", "m", ADA)
        assert await _owner_in_step(spark_db, cid) == ADA
        seen = await spark_db.fetch_value(
            "SELECT last_seen_at FROM conversation_members WHERE conversation_id = ?", (cid,)
        )
        assert seen is not None

    async def test_share_add_change_and_unchanged(self, spark_db: Database) -> None:
        cid = await conversations.create_conversation(spark_db, "c", "m", ADA)
        added = await conversation_members.share(spark_db, ADA, cid, CORA, Role.VIEWER)
        assert added.outcome == "added"
        changed = await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
        assert (changed.outcome, changed.previous_role) == ("changed", Role.VIEWER)
        same = await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
        assert same.outcome == "unchanged"
        roles = [
            (r["user_guid"], r["role"])
            for r in await conversation_members.members(spark_db, CORA, cid)
        ]
        assert roles == [(ADA, "owner"), (CORA, "contributor")]

    async def test_only_the_owner_shares(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        with pytest.raises(NotPermitted):
            await conversation_members.share(spark_db, CORA, cid, SAM, Role.VIEWER)
        with pytest.raises(NotOwned):
            await conversation_members.share(spark_db, SAM, cid, SAM, Role.CONTRIBUTOR)
        with pytest.raises(ValueError):
            await conversation_members.share(spark_db, ADA, cid, SAM, Role.OWNER)
        with pytest.raises(ValueError):
            await conversation_members.share(spark_db, ADA, cid, ADA, Role.VIEWER)
        assert await conversation_role(spark_db, SAM, cid) is None

    async def test_members_are_hidden_from_strangers(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        assert await conversation_members.members(spark_db, SAM, cid) == []
        assert not await conversation_members.is_shared(spark_db, SAM, cid)
        assert await conversation_members.is_shared(spark_db, VIC, cid)

    async def test_remove_and_leave(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        with pytest.raises(NotPermitted):
            await conversation_members.remove_member(spark_db, CORA, cid, VIC)
        assert await conversation_members.remove_member(spark_db, ADA, cid, VIC) is Role.VIEWER
        assert await conversation_role(spark_db, VIC, cid) is None
        assert await messages.get_messages(spark_db, VIC, cid) == []
        assert await conversation_members.remove_member(spark_db, ADA, cid, VIC) is None
        with pytest.raises(ValueError):
            await conversation_members.remove_member(spark_db, ADA, cid, ADA)
        assert await conversation_members.leave(spark_db, CORA, cid) is Role.CONTRIBUTOR
        assert await conversation_role(spark_db, CORA, cid) is None
        with pytest.raises(NotPermitted):
            await conversation_members.leave(spark_db, ADA, cid)
        with pytest.raises(NotOwned):
            await conversation_members.leave(spark_db, SAM, cid)

    async def test_removal_releases_a_held_turn(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        assert await conversation_members.acquire_turn(spark_db, CORA, cid, "s1", "r1") is None
        await conversation_members.remove_member(spark_db, ADA, cid, CORA)
        assert await conversation_members.current_turn(spark_db, ADA, cid) is None

    async def test_lists_new_markers_and_stars(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        own = await conversations.get_active_conversations(spark_db, ADA)
        assert [(c["id"], c["is_shared"]) for c in own] == [(cid, 1)]
        assert await conversations.get_active_conversations(spark_db, VIC) == []
        listed = await conversation_members.shared_with(spark_db, VIC)
        assert [(r["id"], r["role"], r["owner_guid"], r["is_new"]) for r in listed] == [
            (cid, "viewer", ADA, 1)
        ]
        assert listed[0]["message_count"] == 1
        assert await conversation_members.unseen_count(spark_db, VIC) == 1
        await conversation_members.mark_seen(spark_db, VIC, cid)
        assert await conversation_members.unseen_count(spark_db, VIC) == 0
        assert (await conversation_members.shared_with(spark_db, VIC))[0]["is_new"] == 0
        # A member's star is their own: the owner's is unchanged.
        assert await conversation_members.set_favourite(spark_db, VIC, cid, True)
        assert (await conversation_members.shared_with(spark_db, VIC))[0]["is_favourite"] == 1
        assert (await conversations.get_conversation(spark_db, cid, ADA))["is_favourite"] == 0
        assert not await conversation_members.set_favourite(spark_db, SAM, cid, True)
        # A deleted conversation leaves the lists.
        await conversations.delete_conversation(spark_db, cid, ADA)
        assert await conversation_members.shared_with(spark_db, VIC) == []

    async def test_search_finds_shared_conversations(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        assert [c["id"] for c in await conversations.search_conversations(spark_db, "plan", VIC)]
        assert [
            c["id"] for c in await conversations.search_conversations(spark_db, "plan", VIC)
        ] == [cid]
        assert await conversations.search_conversations(spark_db, "plan", SAM) == []


class TestTransfer:
    async def test_transfer_to_a_member(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        other = await conversations.create_conversation(spark_db, "Ada's other", "m", ADA)
        assert await conversation_links.add_link(spark_db, cid, other, ADA)
        assert await conversation_links.add_link(spark_db, other, cid, ADA)
        await conversation_members.set_favourite(spark_db, ADA, cid, True)
        await conversation_members.transfer(spark_db, ADA, cid, CORA)
        assert await _owner_in_step(spark_db, cid) == CORA
        assert await conversation_role(spark_db, ADA, cid) is Role.CONTRIBUTOR
        assert await conversation_role(spark_db, VIC, cid) is Role.VIEWER
        # Links to and from the old owner's other conversations are gone.
        count = await spark_db.fetch_value(
            "SELECT COUNT(*) FROM conversation_links"
            " WHERE source_conversation_id = ? OR target_conversation_id = ?",
            (cid, cid),
        )
        assert count == 0
        # Ada's star stays hers; Cora's list does not inherit it.
        assert (await conversations.get_conversation(spark_db, cid, CORA))["is_favourite"] == 0
        listed = await conversation_members.shared_with(spark_db, ADA)
        assert [(r["id"], r["is_favourite"]) for r in listed] == [(cid, 1)]
        # Only the new owner manages now.
        with pytest.raises(NotPermitted):
            await conversation_members.share(spark_db, ADA, cid, SAM, Role.VIEWER)
        await conversation_members.share(spark_db, CORA, cid, SAM, Role.VIEWER)

    async def test_transfer_adds_a_new_person(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        await conversation_members.transfer(spark_db, ADA, cid, SAM)
        assert await _owner_in_step(spark_db, cid) == SAM
        assert await conversation_role(spark_db, ADA, cid) is Role.CONTRIBUTOR

    async def test_only_the_owner_transfers(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        with pytest.raises(NotPermitted):
            await conversation_members.transfer(spark_db, CORA, cid, CORA)
        with pytest.raises(NotOwned):
            await conversation_members.transfer(spark_db, SAM, cid, SAM)
        with pytest.raises(ValueError):
            await conversation_members.transfer(spark_db, ADA, cid, ADA)
        assert await _owner_in_step(spark_db, cid) == ADA

    async def test_transfer_of_an_owner_without_a_member_row(self, spark_db: Database) -> None:
        cid = int(
            await spark_db.fetch_value(
                "INSERT INTO conversations (name, user_guid) VALUES ('raw', ?) RETURNING id",
                (ADA,),
            )
        )
        await conversation_members.transfer(spark_db, ADA, cid, CORA)
        assert await _owner_in_step(spark_db, cid) == CORA
        assert await conversation_role(spark_db, ADA, cid) is Role.CONTRIBUTOR


class TestRepositoriesByRole:
    async def test_messages(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        mid = await messages.add_message(spark_db, cid, "user", "from cora", 1, CORA)
        sender = await spark_db.fetch_value("SELECT user_guid FROM messages WHERE id = ?", (mid,))
        assert sender == CORA
        with pytest.raises(NotPermitted):
            await messages.add_message(spark_db, cid, "user", "from vic", 1, VIC)
        with pytest.raises(NotOwned):
            await messages.add_message(spark_db, cid, "user", "from sam", 1, SAM)
        assert [m["content"] for m in await messages.get_messages(spark_db, VIC, cid)] == [
            "hello",
            "from cora",
        ]
        assert await messages.get_messages(spark_db, SAM, cid) == []
        await messages.mark_messages_as_rolled_up(spark_db, VIC, cid, [mid])
        assert len(await messages.get_messages(spark_db, ADA, cid)) == 2
        await messages.delete_message(spark_db, CORA, mid, cid)
        assert len(await messages.get_messages(spark_db, ADA, cid)) == 2
        await messages.record_rollup(spark_db, cid, 1, "s", 1, 1, VIC)
        assert await spark_db.fetch_value("SELECT COUNT(*) FROM rollup_history") == 0
        await messages.record_rollup(spark_db, cid, 1, "s", 1, 1, CORA)
        assert await spark_db.fetch_value("SELECT user_guid FROM rollup_history") == CORA

    async def test_conversation_settings_are_the_owners(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        for who in (CORA, VIC, SAM):
            await conversations.update_conversation(spark_db, cid, who, name="renamed")
            await conversations.delete_conversation(spark_db, cid, who)
        conv = await conversations.get_conversation(spark_db, cid, VIC)
        assert conv is not None and conv["name"] == "Ada's plan" and conv["is_active"] == 1
        assert await conversations.get_conversation(spark_db, cid, SAM) is None
        assert await conversations.get_conversation_type(spark_db, VIC, cid) == "standard"
        assert await conversations.get_conversation_type(spark_db, SAM, cid) is None

    async def test_token_totals_follow_the_sender(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        await conversations.update_token_usage(spark_db, cid, "m", 10, 5, VIC)
        await conversations.update_token_usage(spark_db, cid, "m", 10, 5, CORA)
        conv = await conversations.get_conversation(spark_db, cid, ADA)
        assert (conv["tokens_sent"], conv["tokens_received"]) == (10, 5)
        assert await conversations.recalculate_total_tokens(spark_db, VIC, cid) == 0
        assert await conversations.recalculate_total_tokens(spark_db, CORA, cid) == 1

    async def test_tool_permissions_are_each_participants_own(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        await tool_permissions.set_tool_permission(spark_db, cid, "web_search", "allowed", ADA)
        assert await tool_permissions.is_tool_allowed(spark_db, ADA, cid, "web_search") is True
        # Ada's "allow in this conversation" never answers for Cora's turn.
        assert await tool_permissions.is_tool_allowed(spark_db, CORA, cid, "web_search") is None
        await tool_permissions.set_tool_permission(spark_db, cid, "web_search", "denied", CORA)
        assert await tool_permissions.is_tool_allowed(spark_db, CORA, cid, "web_search") is False
        assert await tool_permissions.is_tool_allowed(spark_db, ADA, cid, "web_search") is True
        await tool_permissions.set_tool_permission(spark_db, cid, "web_search", "allowed", VIC)
        assert (
            await tool_permissions.check_tool_permission(spark_db, VIC, cid, "web_search") is None
        )
        assert [
            r["user_guid"]
            for r in await tool_permissions.get_all_tool_permissions(spark_db, CORA, cid)
        ] == [CORA]
        await tool_permissions.delete_tool_permission(spark_db, CORA, cid, "web_search")
        assert await tool_permissions.is_tool_allowed(spark_db, ADA, cid, "web_search") is True

    async def test_tool_switches_are_the_owners_for_every_turn(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        await mcp_ops.set_embedded_tool_enabled(spark_db, cid, "web_search", False, CORA)
        await mcp_ops.set_mcp_server_enabled(spark_db, cid, "srv", False, CORA)
        assert await mcp_ops.is_embedded_tool_enabled(spark_db, ADA, cid, "web_search")
        assert await mcp_ops.is_mcp_server_enabled(spark_db, ADA, cid, "srv")
        await mcp_ops.set_embedded_tool_enabled(spark_db, cid, "web_search", False, ADA)
        await mcp_ops.set_mcp_server_enabled(spark_db, cid, "srv", False, ADA)
        assert not await mcp_ops.is_embedded_tool_enabled(spark_db, CORA, cid, "web_search")
        assert not await mcp_ops.is_mcp_server_enabled(spark_db, CORA, cid, "srv")
        await skills_override(spark_db, cid)

    async def test_tool_activity_agents_and_reasoning(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        tid = await mcp_ops.record_transaction(spark_db, cid, "t", "{}", "r", CORA)
        assert (
            await spark_db.fetch_value(
                "SELECT user_guid FROM mcp_transactions WHERE id = ?", (tid,)
            )
            == CORA
        )
        with pytest.raises(NotOwned):
            await mcp_ops.record_transaction(spark_db, cid, "t", "{}", "r", VIC)
        assert [t["id"] for t in await mcp_ops.get_transactions(spark_db, VIC, cid)] == [tid]
        assert await mcp_ops.get_transactions(spark_db, SAM, cid) == []

        await agents.create_agent_run(spark_db, "a1", cid, "n", "t", "m", "model", CORA)
        with pytest.raises(NotOwned):
            await agents.create_agent_run(spark_db, "a2", cid, "n", "t", "m", "model", VIC)
        await agents.complete_agent_run(spark_db, VIC, "a1", result_text="x")
        assert (await agents.get_agent_run(spark_db, VIC, "a1"))["status"] == "running"
        await agents.complete_agent_run(spark_db, CORA, "a1", result_text="x")
        assert [r["status"] for r in await agents.get_agent_runs(spark_db, VIC, cid)] == [
            "completed"
        ]
        assert await agents.get_agent_runs(spark_db, SAM, cid) == []

        mid = await messages.add_message(spark_db, cid, "assistant", "a", 1, CORA)
        await message_reasoning.add_reasoning(spark_db, VIC, cid, mid, "no")
        assert await message_reasoning.get_reasoning(spark_db, VIC, cid) == {}
        await message_reasoning.add_reasoning(spark_db, CORA, cid, mid, "thought")
        assert await message_reasoning.get_reasoning(spark_db, VIC, cid) == {mid: "thought"}
        assert await message_reasoning.get_reasoning(spark_db, SAM, cid) == {}

    async def test_attachments_and_the_context_index(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        fid = await files.add_file(spark_db, cid, "notes.txt", "txt", 3, CORA, content_text="abc")
        with pytest.raises(NotPermitted):
            await files.add_file(spark_db, cid, "v.txt", "txt", 1, VIC, content_text="v")
        assert [f["id"] for f in await files.get_conversation_files(spark_db, VIC, cid)] == [fid]
        assert await files.get_conversation_files(spark_db, SAM, cid) == []
        await files.delete_file(spark_db, CORA, fid)
        assert len(await files.get_conversation_files(spark_db, ADA, cid)) == 1

        assert await context_index.add_index_element(spark_db, cid, "m", "x", EMB, VIC) is None
        assert await context_index.add_index_element(spark_db, cid, "m", "x", EMB, CORA) is not None
        assert len(await context_index.search_context(spark_db, VIC, [cid], EMB)) == 1
        assert await context_index.search_context(spark_db, SAM, [cid], EMB) == []

    async def test_debates_and_links_stay_the_owners(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        with pytest.raises(NotOwned):
            await debates.create_debate(spark_db, cid, "t", "fixed", 1, CORA, {})
        other = await conversations.create_conversation(spark_db, "Ada's other", "m", ADA)
        await conversation_links.add_link(spark_db, cid, other, ADA)
        assert await conversation_links.get_links(spark_db, cid, CORA) == []
        assert not await conversation_links.add_link(spark_db, cid, other, CORA)


async def skills_override(db: Database, cid: int) -> None:
    await skills.set_conversation_skill_enabled(db, CORA, cid, "s", False)
    assert await skills.get_conversation_skill_states(db, ADA, cid) == {}
    await skills.set_conversation_skill_enabled(db, ADA, cid, "s", False)
    assert await skills.get_conversation_skill_states(db, CORA, cid) == {"s": False}
    assert await skills.get_conversation_skill_states(db, SAM, cid) == {}


class TestTheTurnLock:
    async def test_one_turn_at_a_time(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        assert await conversation_members.acquire_turn(spark_db, ADA, cid, "s1", "r1") is None
        holder = await conversation_members.acquire_turn(spark_db, CORA, cid, "s2", "r2")
        assert holder is not None and holder["user_guid"] == ADA
        current = await conversation_members.current_turn(spark_db, VIC, cid)
        assert current is not None and current["stream_id"] == "s1"
        assert await conversation_members.current_turn(spark_db, SAM, cid) is None
        assert await conversation_members.heartbeat_turn(spark_db, cid, "s1")
        await conversation_members.release_turn(spark_db, cid, "s2")  # not the holder: no-op
        assert await conversation_members.current_turn(spark_db, ADA, cid) is not None
        await conversation_members.release_turn(spark_db, cid, "s1")
        assert await conversation_members.acquire_turn(spark_db, CORA, cid, "s2", "r2") is None

    async def test_viewers_and_strangers_take_no_turn(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        with pytest.raises(NotPermitted):
            await conversation_members.acquire_turn(spark_db, VIC, cid, "s1", "r1")
        with pytest.raises(NotOwned):
            await conversation_members.acquire_turn(spark_db, SAM, cid, "s1", "r1")

    async def test_a_stale_lease_is_taken_over(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        await conversation_members.acquire_turn(spark_db, ADA, cid, "s1", "dead-replica")
        await spark_db.execute(
            "UPDATE conversation_turns SET heartbeat_at = ? WHERE conversation_id = ?",
            ("2000-01-01 00:00:00", cid),
        )
        assert await conversation_members.acquire_turn(spark_db, CORA, cid, "s2", "r2") is None
        assert not await conversation_members.heartbeat_turn(spark_db, cid, "s1")
        holder = await conversation_members.current_turn(spark_db, ADA, cid)
        assert holder is not None and holder["user_guid"] == CORA

    async def test_a_fresh_lease_is_not_taken_over(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        await conversation_members.acquire_turn(spark_db, ADA, cid, "s1", "r1")
        held = await conversation_members.acquire_turn(
            spark_db, CORA, cid, "s2", "r2", stale_after=45
        )
        assert held is not None

    async def test_two_concurrent_senders_one_wins(self, spark_db: Database) -> None:
        cid = await _shared(spark_db)
        barrier = asyncio.Event()

        async def send(who: str, stream: str) -> Any:
            await barrier.wait()
            return await conversation_members.acquire_turn(spark_db, who, cid, stream, "r1")

        tasks = [asyncio.create_task(send(ADA, "a")), asyncio.create_task(send(CORA, "c"))]
        await asyncio.sleep(0)
        barrier.set()
        results = await asyncio.gather(*tasks)
        assert sorted(r is None for r in results) == [False, True]
        rows = await spark_db.fetch_all(
            "SELECT stream_id FROM conversation_turns WHERE conversation_id = ?", (cid,)
        )
        assert len(rows) == 1
