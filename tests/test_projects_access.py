"""The one access decision for projects and the conversations in them (PC9, PC10).

Each test runs on SQLite and (marked postgres) PostgreSQL. Ada owns the project "Plan"
with the sub-project "Detail". It is shared with Cora (Contributor, all conversations),
Vic (Read-only, all conversations), Pat (Contributor, project only) and Rae (Read-only,
project only); Sam is a stranger. Ada starts a conversation in Detail, Pat one in Plan.

Every refusal here was mutation-checked (see the session summary): breaking the role
table, the scope filter, the tree join or the owner rule makes a test here fail.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from spark.database import conversation_members, conversations, messages
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.database.scope import (
    NotOwned,
    NotPermitted,
    Role,
    conversation_role,
)
from spark.db import Database
from spark.projects.access import (
    ProjectNeed,
    ProjectRole,
    ShareScope,
    project_access,
    require_project,
)

ADA = "user-ada-example"
CORA = "user-cora-example"
VIC = "user-vic-example"
PAT = "user-pat-example"
RAE = "user-rae-example"
SAM = "user-sam-example"
EVERYONE = (ADA, CORA, VIC, PAT, RAE, SAM)


@dataclass
class World:
    plan: int
    detail: int
    adas: int
    pats: int


async def build(db: Database) -> World:
    plan = await projects_db.create_project(db, ADA, "Plan", instructions="Be brief.")
    detail = await projects_db.create_project(db, ADA, "Detail", parent_id=plan)
    for who, role, scope in (
        (CORA, ProjectRole.CONTRIBUTOR, ShareScope.ALL_CONVERSATIONS),
        (VIC, ProjectRole.VIEWER, ShareScope.ALL_CONVERSATIONS),
        (PAT, ProjectRole.CONTRIBUTOR, ShareScope.PROJECT_ONLY),
        (RAE, ProjectRole.VIEWER, ShareScope.PROJECT_ONLY),
    ):
        await members_db.share(db, ADA, plan, who, role, scope)
    adas = await conversations.create_conversation(db, "Ada's", "model-a", ADA)
    await projects_db.place_conversation(db, ADA, adas, detail)
    pats = await conversations.create_conversation(db, "Pat's", "model-a", PAT)
    await projects_db.place_conversation(db, PAT, pats, plan)
    return World(plan, detail, adas, pats)


async def roles_in(db: Database, cid: int) -> dict[str, Role | None]:
    return {who: await conversation_role(db, who, cid) for who in EVERYONE}


async def readable(db: Database, cid: int) -> set[str]:
    """Who the READABLE fragment lets read ``cid`` (must agree with conversation_role)."""
    return {who for who in EVERYONE if await conversations.get_conversation(db, cid, who)}


async def can_send(db: Database, cid: int) -> set[str]:
    """Who may add a message (CONTRIBUTE through require_access)."""
    found = set()
    for who in EVERYONE:
        try:
            await messages.add_message(db, cid, "user", "hi", 1, who)
        except NotOwned:
            continue
        found.add(who)
    return found


async def can_compact(db: Database, cid: int) -> set[str]:
    """Who the CONTRIBUTABLE fragment lets mark messages rolled up (a compaction)."""
    found = set()
    for who in EVERYONE:
        mid = await messages.add_message(db, cid, "user", f"from {who}", 1, ADA)
        await messages.mark_messages_as_rolled_up(db, who, cid, [mid])
        rolled = await db.fetch_value("SELECT is_rolled_up FROM messages WHERE id = ?", (mid,))
        if rolled:
            found.add(who)
    return found


class TestProjects:
    async def test_roles_on_the_project_and_its_sub_projects(self, spark_db: Database) -> None:
        w = await build(spark_db)
        for pid in (w.plan, w.detail):
            got = {who: await project_access(spark_db, who, pid) for who in EVERYONE}
            assert {k: (v.role if v else None) for k, v in got.items()} == {
                ADA: ProjectRole.OWNER,
                CORA: ProjectRole.CONTRIBUTOR,
                VIC: ProjectRole.VIEWER,
                PAT: ProjectRole.CONTRIBUTOR,
                RAE: ProjectRole.VIEWER,
                SAM: None,
            }
            assert got[CORA] and got[CORA].scope is ShareScope.ALL_CONVERSATIONS
            assert got[PAT] and got[PAT].scope is ShareScope.PROJECT_ONLY
            assert got[ADA] and got[ADA].scope is None

    @pytest.mark.parametrize(
        ("who", "allowed"),
        [
            (ADA, {ProjectNeed.VIEW, ProjectNeed.CONTRIBUTE, ProjectNeed.MANAGE}),
            (CORA, {ProjectNeed.VIEW, ProjectNeed.CONTRIBUTE}),
            (PAT, {ProjectNeed.VIEW, ProjectNeed.CONTRIBUTE}),
            (VIC, {ProjectNeed.VIEW}),
            (RAE, {ProjectNeed.VIEW}),
            (SAM, set()),
        ],
    )
    async def test_what_each_role_may_do(
        self, spark_db: Database, who: str, allowed: set[ProjectNeed]
    ) -> None:
        w = await build(spark_db)
        for need in ProjectNeed:
            try:
                await require_project(spark_db, who, w.detail, need)
            except NotPermitted:
                assert need not in allowed and who != SAM
                continue
            except NotOwned:
                assert who == SAM
                continue
            assert need in allowed

    async def test_a_stranger_cannot_tell_a_project_exists(self, spark_db: Database) -> None:
        w = await build(spark_db)
        for pid in (w.plan, w.detail, 999_999):
            with pytest.raises(NotOwned) as caught:
                await require_project(spark_db, SAM, pid, ProjectNeed.VIEW)
            assert not isinstance(caught.value, NotPermitted)


class TestConversationsInAProject:
    async def test_all_conversations_shows_every_conversation_at_the_member_role(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        assert await roles_in(spark_db, w.adas) == {
            ADA: Role.OWNER,
            CORA: Role.CONTRIBUTOR,
            VIC: Role.VIEWER,
            PAT: None,
            RAE: None,
            SAM: None,
        }
        assert await readable(spark_db, w.adas) == {ADA, CORA, VIC}
        assert await can_send(spark_db, w.adas) == {ADA, CORA}
        assert await can_compact(spark_db, w.adas) == {ADA, CORA}

    async def test_a_members_conversation_and_the_project_owner(self, spark_db: Database) -> None:
        w = await build(spark_db)
        # Pat (project only) started it: the owner and the all-conversations members see it.
        assert await roles_in(spark_db, w.pats) == {
            ADA: Role.CONTRIBUTOR,
            CORA: Role.CONTRIBUTOR,
            VIC: Role.VIEWER,
            PAT: Role.OWNER,
            RAE: None,
            SAM: None,
        }
        assert await readable(spark_db, w.pats) == {ADA, CORA, VIC, PAT}
        assert await can_send(spark_db, w.pats) == {ADA, CORA, PAT}
        assert await can_compact(spark_db, w.pats) == {ADA, CORA, PAT}

    async def test_live_for_conversations_started_later(self, spark_db: Database) -> None:
        w = await build(spark_db)
        later = await conversations.create_conversation(spark_db, "Later", "model-a", CORA)
        await projects_db.place_conversation(spark_db, CORA, later, w.detail)
        assert await readable(spark_db, later) == {ADA, CORA, VIC}

    async def test_a_removed_member_loses_every_conversation(self, spark_db: Database) -> None:
        w = await build(spark_db)
        await members_db.remove(spark_db, ADA, w.plan, CORA)
        assert await project_access(spark_db, CORA, w.plan) is None
        assert await conversation_role(spark_db, CORA, w.adas) is None
        assert await conversation_role(spark_db, CORA, w.pats) is None

    async def test_a_scope_change_returns_conversations_to_private(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        # Ada also shares her conversation with Cora individually, read-only.
        await conversation_members.share(spark_db, ADA, w.adas, CORA, Role.VIEWER)
        await members_db.share(
            spark_db, ADA, w.plan, CORA, ProjectRole.CONTRIBUTOR, ShareScope.PROJECT_ONLY
        )
        # Pat's conversation is private to Pat again (for Cora); Ada's individual share stays.
        assert await conversation_role(spark_db, CORA, w.pats) is None
        assert await conversation_role(spark_db, CORA, w.adas) is Role.VIEWER
        assert await can_send(spark_db, w.adas) == {ADA}

    async def test_the_better_of_an_individual_share_and_the_project_role(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        await conversation_members.share(spark_db, ADA, w.adas, VIC, Role.CONTRIBUTOR)
        assert await conversation_role(spark_db, VIC, w.adas) is Role.CONTRIBUTOR
        assert VIC in await can_send(spark_db, w.adas)

    async def test_a_conversation_out_of_any_project_is_unaffected(
        self, spark_db: Database
    ) -> None:
        await build(spark_db)
        alone = await conversations.create_conversation(spark_db, "Alone", "model-a", ADA)
        assert await readable(spark_db, alone) == {ADA}

    async def test_managing_stays_with_whoever_started_it(self, spark_db: Database) -> None:
        w = await build(spark_db)
        # The project owner may not rename Pat's conversation, nor share it.
        await conversations.update_conversation(spark_db, w.pats, ADA, name="taken")
        row = await conversations.get_conversation(spark_db, w.pats, PAT)
        assert row is not None and row["name"] == "Pat's"
        with pytest.raises(NotPermitted):
            await conversation_members.share(spark_db, ADA, w.pats, SAM, Role.VIEWER)

    async def test_is_shared_when_anyone_else_sees_it_through_the_project(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        assert await conversation_members.is_shared(spark_db, ADA, w.adas)
        assert await conversation_members.is_shared(spark_db, PAT, w.pats)
        # A project of Ada's shared with nobody: her own conversation there is not shared.
        own = await projects_db.create_project(spark_db, ADA, "Private")
        mine = await conversations.create_conversation(spark_db, "Mine", "model-a", ADA)
        await projects_db.place_conversation(spark_db, ADA, mine, own)
        assert not await conversation_members.is_shared(spark_db, ADA, mine)
