"""Projects in the repositories: levels, edits, conversations and moves, the share limit,
resources, deletion and transfer. Each test runs on SQLite and (marked postgres)
PostgreSQL.

Ada owns "Plan" (with the sub-project "Detail"), shared with Cora (Contributor, all
conversations), Vic (Read-only, all conversations) and Pat (Contributor, project only).
Sam is a stranger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from spark.database import conversation_members, conversations, debates
from spark.database import files as files_db
from spark.database import messages
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.database.projects import DeleteMode, TransferQuota
from spark.database.scope import NotOwned, NotPermitted, Role, conversation_role
from spark.db import Database
from spark.projects.access import (
    DepthLimit,
    ProjectRefused,
    ProjectRole,
    ShareScope,
    project_access,
)

ADA = "user-ada-example"
CORA = "user-cora-example"
VIC = "user-vic-example"
PAT = "user-pat-example"
SAM = "user-sam-example"


@dataclass
class World:
    plan: int
    detail: int


async def build(db: Database) -> World:
    plan = await projects_db.create_project(db, ADA, "Plan", description="The plan")
    detail = await projects_db.create_project(db, ADA, "Detail", parent_id=plan)
    await members_db.share(
        db, ADA, plan, CORA, ProjectRole.CONTRIBUTOR, ShareScope.ALL_CONVERSATIONS
    )
    await members_db.share(db, ADA, plan, VIC, ProjectRole.VIEWER, ShareScope.ALL_CONVERSATIONS)
    await members_db.share(db, ADA, plan, PAT, ProjectRole.CONTRIBUTOR, ShareScope.PROJECT_ONLY)
    return World(plan, detail)


async def talk(db: Database, who: str, name: str, project: int | None = None) -> int:
    cid = await conversations.create_conversation(db, name, "model-a", who)
    if project is not None:
        await projects_db.place_conversation(db, who, cid, project)
    return cid


async def resource(db: Database, owner: str, project: int, adder: str, name: str, size: int) -> int:
    file_id = await files_db.record_user_file(
        db,
        owner,
        files_db.NewUserFile(
            key=f"projects/{project}/{name}", filename=name, size=size, source="project"
        ),
        quota_bytes=None,
    )
    await projects_db.add_resource(db, adder, project, file_id)
    return file_id


class TestLevels:
    async def test_five_levels_and_no_more(self, spark_db: Database) -> None:
        parent = await projects_db.create_project(spark_db, ADA, "Level 1")
        for level in range(2, 6):
            parent = await projects_db.create_project(
                spark_db, ADA, f"Level {level}", parent_id=parent
            )
        access = await project_access(spark_db, ADA, parent)
        assert access is not None and access.depth == 5
        with pytest.raises(DepthLimit, match="at most 5 levels"):
            await projects_db.create_project(spark_db, ADA, "Level 6", parent_id=parent)

    async def test_the_database_refuses_a_sixth_level(self, spark_db: Database) -> None:
        top = await projects_db.create_project(spark_db, ADA, "Top")
        with pytest.raises(Exception):  # noqa: B017 - either engine's integrity error
            await spark_db.execute(
                "INSERT INTO projects (user_guid, parent_id, root_id, depth, name)"
                " VALUES (?, ?, ?, 6, 'deep')",
                (ADA, top, top),
            )

    async def test_only_the_owner_creates_sub_projects(self, spark_db: Database) -> None:
        w = await build(spark_db)
        with pytest.raises(NotPermitted):
            await projects_db.create_project(spark_db, CORA, "Mine", parent_id=w.plan)
        with pytest.raises(NotOwned):
            await projects_db.create_project(spark_db, SAM, "Mine", parent_id=w.plan)
        # A sub-project belongs to the tree's owner.
        sub = await projects_db.create_project(spark_db, ADA, "More", parent_id=w.detail)
        access = await project_access(spark_db, ADA, sub)
        assert access is not None and access.root_id == w.plan and access.depth == 3

    async def test_a_name_is_required_and_bounded(self, spark_db: Database) -> None:
        with pytest.raises(ProjectRefused, match="needs a name"):
            await projects_db.create_project(spark_db, ADA, "   ")
        with pytest.raises(ProjectRefused, match="at most"):
            await projects_db.create_project(spark_db, ADA, "x" * 201)

    async def test_moving_a_sub_project(self, spark_db: Database) -> None:
        w = await build(spark_db)
        other = await projects_db.create_project(spark_db, ADA, "Other", parent_id=w.plan)
        deep = await projects_db.create_project(spark_db, ADA, "Deep", parent_id=w.detail)
        assert await projects_db.move_project(spark_db, ADA, w.detail, other) == (w.plan, other)
        moved = await project_access(spark_db, ADA, deep)
        assert moved is not None and moved.depth == 4
        with pytest.raises(ProjectRefused, match="into itself"):
            await projects_db.move_project(spark_db, ADA, other, deep)
        with pytest.raises(ProjectRefused, match="top-level"):
            await projects_db.move_project(spark_db, ADA, w.plan, other)
        elsewhere = await projects_db.create_project(spark_db, ADA, "Elsewhere")
        with pytest.raises(ProjectRefused, match="within its own"):
            await projects_db.move_project(spark_db, ADA, other, elsewhere)
        with pytest.raises(NotPermitted):
            await projects_db.move_project(spark_db, CORA, other, w.plan)

    async def test_a_move_that_would_go_too_deep(self, spark_db: Database) -> None:
        top = await projects_db.create_project(spark_db, ADA, "Top")
        two = await projects_db.create_project(spark_db, ADA, "Two", parent_id=top)
        three = await projects_db.create_project(spark_db, ADA, "Three", parent_id=two)
        four = await projects_db.create_project(spark_db, ADA, "Four", parent_id=three)
        side = await projects_db.create_project(spark_db, ADA, "Side", parent_id=top)
        under = await projects_db.create_project(spark_db, ADA, "Under", parent_id=side)
        # Side and Under under Four would be levels 5 and 6.
        with pytest.raises(DepthLimit):
            await projects_db.move_project(spark_db, ADA, side, four)
        # Under alone fits at level 5.
        await projects_db.move_project(spark_db, ADA, under, four)
        moved = await project_access(spark_db, ADA, under)
        assert moved is not None and moved.depth == 5


class TestEditsAndLists:
    async def test_only_the_owner_edits(self, spark_db: Database) -> None:
        w = await build(spark_db)
        changed = await projects_db.update_project(
            spark_db, ADA, w.plan, instructions="Cite sources.", name="Plan"
        )
        assert changed == ["instructions"]
        for who, error in ((CORA, NotPermitted), (VIC, NotPermitted), (SAM, NotOwned)):
            with pytest.raises(error):
                await projects_db.update_project(spark_db, who, w.plan, instructions="x")

    async def test_each_person_lists_what_they_may_view(self, spark_db: Database) -> None:
        w = await build(spark_db)
        mine = await projects_db.create_project(spark_db, CORA, "Cora's own")
        ada = await projects_db.list_projects(spark_db, ADA)
        cora = await projects_db.list_projects(spark_db, CORA)
        assert {(r["id"], r["role"]) for r in ada} == {(w.plan, "owner"), (w.detail, "owner")}
        assert {(r["id"], r["role"], r["scope"]) for r in cora} == {
            (w.plan, "contributor", "all_conversations"),
            (w.detail, "contributor", "all_conversations"),
            (mine, "owner", None),
        }
        assert await projects_db.list_projects(spark_db, SAM) == []

    async def test_visible_conversations(self, spark_db: Database) -> None:
        w = await build(spark_db)
        adas = await talk(spark_db, ADA, "Ada's", w.detail)
        pats = await talk(spark_db, PAT, "Pat's", w.plan)
        assert {r["id"] for r in await projects_db.visible_conversations(spark_db, VIC)} == {
            adas,
            pats,
        }
        assert {r["id"] for r in await projects_db.visible_conversations(spark_db, PAT)} == {pats}
        assert await projects_db.visible_conversations(spark_db, SAM) == []
        assert [
            r["id"] for r in await projects_db.project_conversations(spark_db, CORA, w.detail)
        ] == [adas]


class TestStartingConversations:
    async def test_a_contributor_starts_one_anywhere_in_the_tree(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, PAT, "Pat's", w.detail)
        row = await conversations.get_conversation(spark_db, cid, PAT)
        assert row is not None and row["project_id"] == w.detail

    async def test_read_only_and_strangers_cannot(self, spark_db: Database) -> None:
        w = await build(spark_db)
        with pytest.raises(NotPermitted):
            await talk(spark_db, VIC, "Vic's", w.plan)
        with pytest.raises(NotOwned):
            await talk(spark_db, SAM, "Sam's", w.plan)

    async def test_only_a_new_conversation_is_placed(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Old")
        await messages.add_message(spark_db, cid, "user", "hello", 1, ADA)
        with pytest.raises(ProjectRefused, match="new conversation"):
            await projects_db.place_conversation(spark_db, ADA, cid, w.plan)


class TestMoves:
    async def test_into_a_shared_project_asks_first_and_names_who(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's")
        report = await projects_db.move_conversation(spark_db, ADA, cid, w.plan)
        assert report.confirm_required and not report.moved
        assert report.widened == {CORA: "contributor", VIC: "viewer"}
        assert report.narrowed == {}
        assert await conversation_role(spark_db, CORA, cid) is None  # nothing moved
        done = await projects_db.move_conversation(spark_db, ADA, cid, w.plan, confirm=True)
        assert done.moved
        assert await conversation_role(spark_db, CORA, cid) is Role.CONTRIBUTOR

    async def test_into_an_unshared_project_needs_no_confirmation(self, spark_db: Database) -> None:
        own = await projects_db.create_project(spark_db, ADA, "Private")
        cid = await talk(spark_db, ADA, "Ada's")
        report = await projects_db.move_conversation(spark_db, ADA, cid, own)
        assert report.moved and not report.confirm_required

    async def test_between_levels_of_one_project(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's", w.plan)
        report = await projects_db.move_conversation(spark_db, ADA, cid, w.detail)
        assert report.moved and report.widened == {} and report.narrowed == {}

    async def test_out_to_no_project_narrows(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's", w.plan)
        report = await projects_db.move_conversation(spark_db, ADA, cid, None)
        assert report.confirm_required
        assert report.narrowed == {CORA: None, VIC: None}
        await projects_db.move_conversation(spark_db, ADA, cid, None, confirm=True)
        assert await conversation_role(spark_db, VIC, cid) is None

    async def test_a_contributor_takes_their_own_out(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, PAT, "Pat's", w.plan)
        report = await projects_db.move_conversation(spark_db, PAT, cid, None, confirm=True)
        assert report.moved
        assert report.narrowed == {ADA: None, CORA: None, VIC: None}
        assert await conversation_role(spark_db, ADA, cid) is None

    async def test_between_projects_only_through_no_project(self, spark_db: Database) -> None:
        w = await build(spark_db)
        other = await projects_db.create_project(spark_db, ADA, "Other")
        cid = await talk(spark_db, ADA, "Ada's", w.plan)
        with pytest.raises(ProjectRefused, match="through no project"):
            await projects_db.move_conversation(spark_db, ADA, cid, other, confirm=True)

    async def test_refusals(self, spark_db: Database) -> None:
        w = await build(spark_db)
        coras = await talk(spark_db, CORA, "Cora's")
        # Into a project the mover does not own.
        with pytest.raises(NotPermitted, match="project you own"):
            await projects_db.move_conversation(spark_db, CORA, coras, w.plan, confirm=True)
        # A conversation the mover did not start (Ada sees Pat's through the project).
        pats = await talk(spark_db, PAT, "Pat's", w.plan)
        with pytest.raises(NotPermitted, match="started"):
            await projects_db.move_conversation(spark_db, ADA, pats, w.detail, confirm=True)
        with pytest.raises(NotOwned):
            await projects_db.move_conversation(spark_db, SAM, pats, None, confirm=True)
        with pytest.raises(ProjectRefused, match="already there"):
            await projects_db.move_conversation(spark_db, PAT, pats, w.plan, confirm=True)

    async def test_a_debate_is_not_placed(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Debate")
        await conversations.update_conversation(spark_db, cid, ADA, conversation_type="debate")
        await debates.create_debate(spark_db, cid, "topic", "fixed", 1, ADA, {})
        with pytest.raises(ProjectRefused, match="Debates"):
            await projects_db.move_conversation(spark_db, ADA, cid, w.plan, confirm=True)

    async def test_individual_shares_outside_the_project_end(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's")
        await conversation_members.share(spark_db, ADA, cid, SAM, Role.VIEWER)
        await conversation_members.share(spark_db, ADA, cid, PAT, Role.VIEWER)
        report = await projects_db.move_conversation(spark_db, ADA, cid, w.plan)
        assert report.confirm_required
        assert report.shares_removed == [SAM]
        assert report.narrowed == {SAM: None}
        await projects_db.move_conversation(spark_db, ADA, cid, w.plan, confirm=True)
        assert await conversation_role(spark_db, SAM, cid) is None
        assert await conversation_role(spark_db, PAT, cid) is Role.VIEWER


class TestTheShareLimit:
    async def test_a_conversation_in_a_project_is_shared_only_inside_it(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's", w.plan)
        with pytest.raises(ValueError, match="access to the project"):
            await conversation_members.share(spark_db, ADA, cid, SAM, Role.VIEWER)
        with pytest.raises(ValueError, match="access to the project"):
            await conversation_members.transfer(spark_db, ADA, cid, SAM)
        change = await conversation_members.share(spark_db, ADA, cid, PAT, Role.CONTRIBUTOR)
        assert change.outcome == "added"
        # A member's own conversation may be shared with the project's owner.
        pats = await talk(spark_db, PAT, "Pat's", w.plan)
        assert (await conversation_members.share(spark_db, PAT, pats, VIC, Role.VIEWER)).outcome
        # Out of any project, no limit.
        alone = await talk(spark_db, ADA, "Alone")
        assert (await conversation_members.share(spark_db, ADA, alone, SAM, Role.VIEWER)).outcome

    async def test_a_removed_member_loses_individual_shares_in_the_tree(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's", w.plan)
        await conversation_members.share(spark_db, ADA, cid, PAT, Role.VIEWER)
        pats = await talk(spark_db, PAT, "Pat's", w.plan)
        change = await members_db.remove(spark_db, ADA, w.plan, PAT)
        assert change.outcome == "removed" and change.lost == (cid,)
        assert await conversation_role(spark_db, PAT, cid) is None
        # Their own conversation stays theirs, still in the project.
        assert await conversation_role(spark_db, PAT, pats) is Role.OWNER

    async def test_members_are_the_owners_to_manage(self, spark_db: Database) -> None:
        w = await build(spark_db)
        with pytest.raises(NotPermitted):
            await members_db.share(
                spark_db, CORA, w.plan, SAM, ProjectRole.VIEWER, ShareScope.PROJECT_ONLY
            )
        with pytest.raises(NotPermitted):
            await members_db.members(spark_db, CORA, w.plan)
        with pytest.raises(ProjectRefused, match="top-level"):
            await members_db.share(
                spark_db, ADA, w.detail, SAM, ProjectRole.VIEWER, ShareScope.PROJECT_ONLY
            )
        with pytest.raises(ProjectRefused):
            await members_db.share(
                spark_db, ADA, w.plan, ADA, ProjectRole.VIEWER, ShareScope.PROJECT_ONLY
            )
        listed = await members_db.members(spark_db, ADA, w.plan)
        assert {(r["user_guid"], r["role"], r["scope"]) for r in listed} == {
            (CORA, "contributor", "all_conversations"),
            (VIC, "viewer", "all_conversations"),
            (PAT, "contributor", "project_only"),
        }
        left = await members_db.leave(spark_db, VIC, w.plan)
        assert left.outcome == "removed"
        with pytest.raises(NotPermitted):
            await members_db.leave(spark_db, ADA, w.plan)

    async def test_a_narrowed_scope_reports_what_was_lost(self, spark_db: Database) -> None:
        w = await build(spark_db)
        cid = await talk(spark_db, ADA, "Ada's", w.plan)
        change = await members_db.share(
            spark_db, ADA, w.plan, VIC, ProjectRole.VIEWER, ShareScope.PROJECT_ONLY
        )
        assert change.outcome == "changed" and change.lost == (cid,)
        assert change.previous_scope is ShareScope.ALL_CONVERSATIONS


class TestResources:
    async def test_contributors_add_and_remove_only_their_own(self, spark_db: Database) -> None:
        w = await build(spark_db)
        adas = await resource(spark_db, ADA, w.plan, ADA, "brief.txt", 10)
        coras = await resource(spark_db, ADA, w.detail, CORA, "notes.txt", 20)
        listed = await projects_db.list_resources(spark_db, VIC, w.detail)
        assert [(r["file_id"], r["added_by"]) for r in listed] == [(coras, CORA)]
        with pytest.raises(NotPermitted):
            await projects_db.remove_resource(spark_db, CORA, w.plan, adas)
        with pytest.raises(NotPermitted):
            await projects_db.remove_resource(spark_db, VIC, w.detail, coras)
        with pytest.raises(NotPermitted):
            await projects_db.add_resource(spark_db, VIC, w.plan, adas)
        gone = await projects_db.remove_resource(spark_db, CORA, w.detail, coras)
        assert gone is not None and int(gone["id"]) == coras
        assert await files_db.get_usage(spark_db, ADA) == 10  # the owner's charge released
        with pytest.raises(NotOwned):
            await projects_db.remove_resource(spark_db, ADA, w.detail, coras)

    async def test_a_resource_is_a_file_of_the_owner(self, spark_db: Database) -> None:
        w = await build(spark_db)
        coras_own = await files_db.record_user_file(
            spark_db,
            CORA,
            files_db.NewUserFile(key="uploads/x/own.txt", filename="own.txt", size=3),
            quota_bytes=None,
        )
        with pytest.raises(ProjectRefused, match="owner's file area"):
            await projects_db.add_resource(spark_db, CORA, w.plan, coras_own)

    async def test_a_conversation_inherits_every_level(self, spark_db: Database) -> None:
        w = await build(spark_db)
        top = await resource(spark_db, ADA, w.plan, ADA, "top.txt", 1)
        low = await resource(spark_db, ADA, w.detail, ADA, "low.txt", 1)
        other = await projects_db.create_project(spark_db, ADA, "Other")
        stray = await resource(spark_db, ADA, other, ADA, "stray.txt", 1)
        cid = await talk(spark_db, CORA, "Cora's", w.detail)
        levels = await projects_db.conversation_resources(spark_db, CORA, cid)
        assert [(lv.name, lv.level, [r["file_id"] for r in lv.resources]) for lv in levels] == [
            ("Plan", 1, [top]),
            ("Detail", 2, [low]),
        ]
        assert await projects_db.resource_for_conversation(spark_db, VIC, cid, top) is not None
        assert await projects_db.resource_for_conversation(spark_db, CORA, cid, stray) is None
        assert await projects_db.resource_for_conversation(spark_db, PAT, cid, top) is None
        assert await projects_db.resource_for_conversation(spark_db, SAM, cid, top) is None
        upper = await talk(spark_db, ADA, "Ada's", w.plan)
        assert await projects_db.resource_for_conversation(spark_db, ADA, upper, low) is None


class TestDeletion:
    async def test_the_preview_counts(self, spark_db: Database) -> None:
        w = await build(spark_db)
        await talk(spark_db, ADA, "Ada's", w.plan)
        await talk(spark_db, PAT, "Pat's", w.detail)
        await talk(spark_db, CORA, "Cora's", w.detail)
        await resource(spark_db, ADA, w.detail, CORA, "a.txt", 5)
        preview = await projects_db.delete_preview(spark_db, ADA, w.plan)
        assert preview.summary() == {
            "name": "Plan",
            "sub_projects": 1,
            "conversations": 3,
            "conversations_by_others": 2,
            "resources": 1,
            "resource_bytes": 5,
            "members": 3,
            "people_told": 2,
        }
        with pytest.raises(NotPermitted):
            await projects_db.delete_preview(spark_db, CORA, w.plan)

    async def test_keep_a_top_level_projects_conversations(self, spark_db: Database) -> None:
        w = await build(spark_db)
        adas = await talk(spark_db, ADA, "Ada's", w.detail)
        pats = await talk(spark_db, PAT, "Pat's", w.plan)
        await conversation_members.share(spark_db, ADA, adas, PAT, Role.VIEWER)
        file_id = await resource(spark_db, ADA, w.plan, CORA, "a.txt", 5)
        result = await projects_db.delete_project(spark_db, ADA, w.plan, DeleteMode.KEEP)
        assert set(result.conversations_kept) == {adas, pats}
        assert [int(f["id"]) for f in result.files] == [file_id]
        assert await files_db.get_usage(spark_db, ADA) == 0
        assert await project_access(spark_db, ADA, w.detail) is None
        for cid, starter in ((adas, ADA), (pats, PAT)):
            row = await conversations.get_conversation(spark_db, cid, starter)
            assert row is not None and row["project_id"] is None and row["is_active"] == 1
        # Private to whoever started each, except an individual share.
        assert await conversation_role(spark_db, CORA, adas) is None
        assert await conversation_role(spark_db, ADA, pats) is None
        assert await conversation_role(spark_db, PAT, adas) is Role.VIEWER
        assert result.told == {}

    async def test_keep_a_sub_projects_conversations_moves_them_up(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        deeper = await projects_db.create_project(spark_db, ADA, "Deeper", parent_id=w.detail)
        here = await talk(spark_db, ADA, "Here", w.detail)
        below = await talk(spark_db, CORA, "Below", deeper)
        await projects_db.delete_project(spark_db, ADA, w.detail, DeleteMode.KEEP)
        for cid, who in ((here, ADA), (below, CORA)):
            row = await conversations.get_conversation(spark_db, cid, who)
            assert row is not None and row["project_id"] == w.plan
        assert await project_access(spark_db, ADA, deeper) is None
        assert await project_access(spark_db, ADA, w.plan) is not None

    async def test_everything_deletes_others_conversations_and_tells_them(
        self, spark_db: Database
    ) -> None:
        w = await build(spark_db)
        adas = await talk(spark_db, ADA, "Ada's", w.plan)
        pats = await talk(spark_db, PAT, "Pat's", w.detail)
        pats2 = await talk(spark_db, PAT, "Pat's too", w.plan)
        result = await projects_db.delete_project(spark_db, ADA, w.plan, DeleteMode.EVERYTHING)
        assert set(result.conversations_deleted) == {adas, pats, pats2}
        assert result.told == {PAT: 2}
        for cid in (adas, pats, pats2):
            active = await spark_db.fetch_value(
                "SELECT is_active FROM conversations WHERE id = ?", (cid,)
            )
            assert active == 0
        told = await projects_db.notices(spark_db, PAT)
        assert [(n["kind"], n["subject"], n["count"]) for n in told] == [
            ("project_deleted", "Plan", 2)
        ]
        assert await projects_db.mark_notices_seen(spark_db, PAT) == 1
        assert await projects_db.notices(spark_db, PAT) == []
        assert await projects_db.notices(spark_db, ADA) == []

    async def test_only_the_owner_deletes(self, spark_db: Database) -> None:
        w = await build(spark_db)
        with pytest.raises(NotPermitted):
            await projects_db.delete_project(spark_db, CORA, w.plan, DeleteMode.KEEP)
        with pytest.raises(NotOwned):
            await projects_db.delete_project(spark_db, SAM, w.plan, DeleteMode.KEEP)


class TestTransfer:
    async def test_the_new_owner_takes_the_tree_files_and_usage(self, spark_db: Database) -> None:
        w = await build(spark_db)
        adas = await talk(spark_db, ADA, "Ada's", w.plan)
        file_id = await resource(spark_db, ADA, w.detail, CORA, "a.txt", 7)
        copied: list[Any] = []

        async def copy(files: Any) -> None:
            copied.extend(int(f["id"]) for f in files)

        result = await projects_db.transfer_project(
            spark_db, ADA, w.plan, CORA, quota_bytes=100, copy_files=copy
        )
        assert copied == [file_id] and [int(f["id"]) for f in result.files] == [file_id]
        assert await files_db.get_usage(spark_db, ADA) == 0
        assert await files_db.get_usage(spark_db, CORA) == 7
        assert await files_db.get_user_file(spark_db, CORA, file_id) is not None
        for pid in (w.plan, w.detail):
            new = await project_access(spark_db, CORA, pid)
            old = await project_access(spark_db, ADA, pid)
            assert new is not None and new.role is ProjectRole.OWNER
            assert old is not None and old.role is ProjectRole.CONTRIBUTOR
            assert old.scope is ShareScope.ALL_CONVERSATIONS
        # Conversations stay with whoever started them.
        assert await conversation_role(spark_db, ADA, adas) is Role.OWNER
        assert [n["kind"] for n in await projects_db.notices(spark_db, CORA)] == [
            "project_transferred"
        ]

    async def test_refused_without_the_quota_and_nothing_changes(self, spark_db: Database) -> None:
        w = await build(spark_db)
        await resource(spark_db, ADA, w.plan, ADA, "big.txt", 50)
        with pytest.raises(TransferQuota, match="storage"):
            await projects_db.transfer_project(spark_db, ADA, w.plan, CORA, quota_bytes=40)
        access = await project_access(spark_db, ADA, w.plan)
        assert access is not None and access.role is ProjectRole.OWNER
        assert await files_db.get_usage(spark_db, ADA) == 50
        assert await files_db.get_usage(spark_db, CORA) == 0

    async def test_a_failed_copy_changes_nothing(self, spark_db: Database) -> None:
        w = await build(spark_db)
        await resource(spark_db, ADA, w.plan, ADA, "a.txt", 5)

        async def broken(files: Any) -> None:
            raise OSError("disk full")

        with pytest.raises(OSError):
            await projects_db.transfer_project(
                spark_db, ADA, w.plan, CORA, quota_bytes=None, copy_files=broken
            )
        access = await project_access(spark_db, ADA, w.plan)
        assert access is not None and access.role is ProjectRole.OWNER

    async def test_to_a_member_or_someone_added_in_the_same_step(self, spark_db: Database) -> None:
        w = await build(spark_db)
        with pytest.raises(ProjectRefused, match="member"):
            await projects_db.transfer_project(spark_db, ADA, w.plan, SAM, quota_bytes=None)
        result = await projects_db.transfer_project(
            spark_db, ADA, w.plan, SAM, quota_bytes=None, add_member=True
        )
        assert result.added_member
        with pytest.raises(NotPermitted):
            await projects_db.transfer_project(spark_db, CORA, w.plan, VIC, quota_bytes=None)
        with pytest.raises(ProjectRefused, match="top-level"):
            await projects_db.transfer_project(spark_db, SAM, w.detail, ADA, quota_bytes=None)
