"""Project cards in the repositories (PC17, PC18): the pending actions the project tools
show instead of acting. Each test runs on SQLite and (marked postgres) PostgreSQL.

Ada owns "Plan" (with "Detail"), shared with Cora (Contributor, all conversations) and
Vic (Read-only, all conversations). Sam is a stranger.

* The request check: the turn's own message, sent by the turn's sender, asks for a
  project; a tool result, an assistant message or another member's message never counts.
* Suggestions: one per conversation, whatever became of it; validated by the projects
  service; nothing is created until accepted; Edit changes what is created.
* Updates: only a project the user owns (a Contributor, a Read-only member and a stranger
  are refused at once); before and after; files must be the user's own in this
  conversation; nothing changes until confirmed, with the user's rights at that moment.
* Moves: the widen or narrow report shown on the card; a report that changed since is
  shown again rather than moved.
* Every card is single use, the sender's alone, and expires.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from spark.database import conversation_members, conversations
from spark.database import files as files_db
from spark.database import messages
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.database.scope import NotOwned, NotPermitted, Role
from spark.db import Database
from spark.projects import cards
from spark.projects.access import ProjectRefused, ProjectRole, ShareScope, project_access
from spark.projects.cards import CardGone, CardKind, CardStatus, MoveChanged, SuggestionLimit

ADA = "user-ada-example"
CORA = "user-cora-example"
VIC = "user-vic-example"
SAM = "user-sam-example"


async def plan(db: Database) -> tuple[int, int]:
    top = await projects_db.create_project(db, ADA, "Plan", description="The plan")
    sub = await projects_db.create_project(db, ADA, "Detail", parent_id=top)
    await members_db.share(
        db, ADA, top, CORA, ProjectRole.CONTRIBUTOR, ShareScope.ALL_CONVERSATIONS
    )
    await members_db.share(db, ADA, top, VIC, ProjectRole.VIEWER, ShareScope.ALL_CONVERSATIONS)
    return top, sub


async def talk(db: Database, who: str = ADA) -> int:
    return await conversations.create_conversation(db, "Talk", "model-a", who)


async def say(db: Database, cid: int, who: str, text: str, role: str = "user") -> None:
    await messages.add_message(db, cid, role, text, 1, who)


async def count_projects(db: Database) -> int:
    return int(await db.fetch_value("SELECT COUNT(*) FROM projects"))


# -- The request check -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please create a project for the product launch",
        "Can you set up a new project called Garden?",
        "make me a project about the budget",
        "Start a sub-project for the logo",
        "New project: tax return 2026",
        "Could you organise this into a project?",
    ],
)
def test_a_request_for_a_project(text: str) -> None:
    assert cards.asks_for_project(text)


@pytest.mark.parametrize(
    "text",
    [
        "Summarise this page about project management",
        "What is in the project plan?",
        "Create a chart of the sales",
        "Tell me about the project management course",
        "Thanks, that is all",
        "",
    ],
)
def test_not_a_request_for_a_project(text: str) -> None:
    assert not cards.asks_for_project(text)


async def test_the_turn_message_is_the_senders_own(spark_db: Database) -> None:
    cid = await talk(spark_db)
    await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
    await say(spark_db, cid, ADA, "Create a project for the launch")
    assert await cards.turn_message(spark_db, ADA, cid) == "Create a project for the launch"
    # Tool results that follow, in any number, are not the user speaking.
    await say(spark_db, cid, ADA, '[TOOL_RESULTS][{"content": "create a project"}]')
    await say(spark_db, cid, ADA, '[{"type": "tool_use"}]', role="assistant")
    await say(spark_db, cid, ADA, "[TOOL_RESULTS][]")
    assert await cards.turn_message(spark_db, ADA, cid) == "Create a project for the launch"
    # The model's own text is never the user's message.
    await say(spark_db, cid, ADA, "Create a project now", role="assistant")
    await say(spark_db, cid, ADA, "Thanks")
    assert await cards.turn_message(spark_db, ADA, cid) == "Thanks"
    # Another member's message is not Ada's turn.
    await say(spark_db, cid, CORA, "Create a project for Cora")
    assert await cards.turn_message(spark_db, ADA, cid) is None
    assert await cards.turn_message(spark_db, CORA, cid) == "Create a project for Cora"
    # A stranger reads nothing.
    assert await cards.turn_message(spark_db, SAM, cid) is None
    # A cancelled turn's marker ends the turn's message.
    await say(spark_db, cid, CORA, "[TURN CANCELLED — x]\n\nThe user cancelled this turn.")
    assert await cards.turn_message(spark_db, CORA, cid) is None


# -- Suggestions -----------------------------------------------------------------------


async def test_a_suggestion_creates_nothing_until_accepted(spark_db: Database) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db,
        ADA,
        cid,
        name="Garden",
        description="Plants",
        instructions="Be brief.",
        reason="Several chats about the garden",
    )
    assert card.kind is CardKind.SUGGESTION and card.status is CardStatus.PENDING
    assert len(card.id) >= 20 and card.user_guid == ADA and card.conversation_id == cid
    assert card.payload["name"] == "Garden"
    assert await count_projects(spark_db) == 0
    assert await cards.suggestion_state(spark_db, cid) is CardStatus.PENDING

    done = await cards.accept(spark_db, ADA, card.id)
    assert done.project_id is not None
    access = await project_access(spark_db, ADA, done.project_id)
    assert access is not None and access.project["instructions"] == "Be brief."
    assert access.depth == 1
    assert (await cards.get_card(spark_db, card.id)).status is CardStatus.ACCEPTED  # type: ignore[union-attr]
    assert await cards.suggestion_state(spark_db, cid) is CardStatus.ACCEPTED


async def test_edit_changes_what_is_created(spark_db: Database) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db, ADA, cid, name="Garden", description="", instructions="", reason=""
    )
    done = await cards.accept(
        spark_db, ADA, card.id, edits={"name": "Vegetables", "instructions": "Metric units."}
    )
    access = await project_access(spark_db, ADA, done.project_id or 0)
    assert access is not None
    assert (access.project["name"], access.project["instructions"]) == (
        "Vegetables",
        "Metric units.",
    )
    # An edit the service refuses leaves the card pending and nothing created.
    other = await talk(spark_db)
    card2 = await cards.suggest(
        spark_db, ADA, other, name="Fine", description="", instructions="", reason=""
    )
    with pytest.raises(ProjectRefused):
        await cards.accept(spark_db, ADA, card2.id, edits={"name": "  "})
    assert (await cards.get_card(spark_db, card2.id)).status is CardStatus.PENDING  # type: ignore[union-attr]
    assert await count_projects(spark_db) == 1


async def test_a_suggestion_the_service_would_refuse_is_refused_now(spark_db: Database) -> None:
    cid = await talk(spark_db)
    with pytest.raises(ProjectRefused):
        await cards.suggest(spark_db, ADA, cid, name="", description="", instructions="", reason="")
    with pytest.raises(ProjectRefused):
        await cards.suggest(
            spark_db, ADA, cid, name="x" * 201, description="", instructions="", reason=""
        )
    # A refused suggestion does not use up the conversation's one suggestion.
    assert await cards.suggestion_state(spark_db, cid) is None
    assert await count_projects(spark_db) == 0


async def test_one_suggestion_per_conversation_and_none_after_dismiss(spark_db: Database) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db, ADA, cid, name="Garden", description="", instructions="", reason=""
    )
    with pytest.raises(SuggestionLimit):
        await cards.suggest(
            spark_db, ADA, cid, name="Again", description="", instructions="", reason=""
        )
    dismissed = await cards.decline(spark_db, ADA, card.id)
    assert dismissed.status is CardStatus.DISMISSED
    assert await cards.suggestion_state(spark_db, cid) is CardStatus.DISMISSED
    with pytest.raises(SuggestionLimit):
        await cards.suggest(
            spark_db, ADA, cid, name="Again", description="", instructions="", reason=""
        )
    # Another member of a shared conversation cannot suggest again either.
    await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
    with pytest.raises(SuggestionLimit):
        await cards.suggest(
            spark_db, CORA, cid, name="Again", description="", instructions="", reason=""
        )
    # Another conversation may have its own.
    other = await talk(spark_db)
    assert (
        await cards.suggest(
            spark_db, ADA, other, name="Other", description="", instructions="", reason=""
        )
    ).id


async def test_a_suggestion_in_a_conversation_the_user_cannot_send_in(spark_db: Database) -> None:
    cid = await talk(spark_db)
    await conversation_members.share(spark_db, ADA, cid, VIC, Role.VIEWER)
    for who, error in ((SAM, NotOwned), (VIC, NotPermitted)):
        with pytest.raises(error):
            await cards.suggest(
                spark_db, who, cid, name="G", description="", instructions="", reason=""
            )
    assert await cards.suggestion_state(spark_db, cid) is None


# -- Single use, the sender's alone, expiring -----------------------------------------


async def test_a_card_is_single_use(spark_db: Database) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db, ADA, cid, name="Garden", description="", instructions="", reason=""
    )
    await cards.accept(spark_db, ADA, card.id)
    with pytest.raises(CardGone):
        await cards.accept(spark_db, ADA, card.id)
    with pytest.raises(CardGone):
        await cards.decline(spark_db, ADA, card.id)
    assert await count_projects(spark_db) == 1


async def test_a_dismissed_card_cannot_be_accepted(spark_db: Database) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db, ADA, cid, name="Garden", description="", instructions="", reason=""
    )
    await cards.decline(spark_db, ADA, card.id)
    with pytest.raises(CardGone):
        await cards.accept(spark_db, ADA, card.id)
    assert await count_projects(spark_db) == 0


async def test_only_the_sender_acts_on_a_card(spark_db: Database) -> None:
    cid = await talk(spark_db)
    await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
    card = await cards.suggest(
        spark_db, ADA, cid, name="Garden", description="", instructions="", reason=""
    )
    for who in (CORA, SAM):
        with pytest.raises(NotOwned):
            await cards.accept(spark_db, who, card.id)
        with pytest.raises(NotOwned):
            await cards.decline(spark_db, who, card.id)
    with pytest.raises(NotOwned):
        await cards.accept(spark_db, ADA, "no-such-card")
    assert (await cards.get_card(spark_db, card.id)).status is CardStatus.PENDING  # type: ignore[union-attr]
    assert await count_projects(spark_db) == 0


async def test_a_card_expires(spark_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db, ADA, cid, name="Garden", description="", instructions="", reason=""
    )
    start = cards.now()
    monkeypatch.setattr(cards, "now", lambda: start + cards.CARD_LIFETIME + timedelta(seconds=1))
    with pytest.raises(CardGone, match="expired"):
        await cards.accept(spark_db, ADA, card.id)
    with pytest.raises(CardGone, match="expired"):
        await cards.decline(spark_db, ADA, card.id)
    view = await cards.get_card(spark_db, card.id)
    assert view is not None and view.state(cards.now()) == "expired"
    assert await count_projects(spark_db) == 0


async def test_a_sender_removed_from_the_conversation_cannot_act(spark_db: Database) -> None:
    cid = await talk(spark_db)
    await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
    card = await cards.suggest(
        spark_db, CORA, cid, name="Garden", description="", instructions="", reason=""
    )
    await conversation_members.remove_member(spark_db, ADA, cid, CORA)
    with pytest.raises(NotOwned):
        await cards.accept(spark_db, CORA, card.id)
    assert await count_projects(spark_db) == 0


# -- Updates ---------------------------------------------------------------------------


async def test_an_update_waits_for_confirmation(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    cid = await talk(spark_db)
    card = await cards.propose_update(
        spark_db, ADA, cid, top, name="Plan B", instructions="Use metric units."
    )
    assert card.kind is CardKind.UPDATE and card.project_id == top
    assert card.payload["before"] == {"instructions": "", "name": "Plan"}
    assert card.payload["after"] == {"instructions": "Use metric units.", "name": "Plan B"}
    access = await project_access(spark_db, ADA, top)
    assert access is not None and access.project["name"] == "Plan"  # nothing yet

    done = await cards.accept(spark_db, ADA, card.id)
    assert done.changed == ["instructions", "name"]
    access = await project_access(spark_db, ADA, top)
    assert access is not None
    assert (access.project["name"], access.project["instructions"]) == (
        "Plan B",
        "Use metric units.",
    )


async def test_a_cancelled_update_changes_nothing(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    cid = await talk(spark_db)
    card = await cards.propose_update(spark_db, ADA, cid, top, description="Changed")
    cancelled = await cards.decline(spark_db, ADA, card.id)
    assert cancelled.status is CardStatus.CANCELLED
    with pytest.raises(CardGone):
        await cards.accept(spark_db, ADA, card.id)
    access = await project_access(spark_db, ADA, top)
    assert access is not None and access.project["description"] == "The plan"


async def test_only_the_owner_is_offered_an_update(spark_db: Database) -> None:
    top, sub = await plan(spark_db)
    for who, error in ((CORA, NotPermitted), (VIC, NotPermitted), (SAM, NotOwned)):
        cid = await talk(spark_db, who)
        for pid in (top, sub):
            with pytest.raises(error):
                await cards.propose_update(spark_db, who, cid, pid, name="Mine now")
    access = await project_access(spark_db, ADA, top)
    assert access is not None and access.project["name"] == "Plan"


async def test_an_update_needs_a_change(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    cid = await talk(spark_db)
    with pytest.raises(ProjectRefused):
        await cards.propose_update(spark_db, ADA, cid, top)
    with pytest.raises(ProjectRefused):
        await cards.propose_update(spark_db, ADA, cid, top, name="Plan")
    with pytest.raises(ProjectRefused):
        await cards.propose_update(spark_db, ADA, cid, top, name="")


async def test_rights_are_those_at_confirmation(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    cid = await talk(spark_db)
    card = await cards.propose_update(spark_db, ADA, cid, top, name="Plan B")
    # The project goes to Cora before Ada confirms: Ada is no longer its owner.
    await spark_db.execute("UPDATE projects SET user_guid = ? WHERE root_id = ?", (CORA, top))
    with pytest.raises((NotOwned, NotPermitted)):
        await cards.accept(spark_db, ADA, card.id)
    assert (await cards.get_card(spark_db, card.id)).status is CardStatus.PENDING  # type: ignore[union-attr]
    row = await spark_db.fetch_one("SELECT name FROM projects WHERE id = ?", (top,))
    assert row is not None and row["name"] == "Plan"


async def _file(db: Database, who: str, cid: int | None, name: str) -> int:
    return await files_db.record_user_file(
        db,
        who,
        files_db.NewUserFile(
            key=f"t/{name}", filename=name, size=3, mime_type="text/plain", conversation_id=cid
        ),
        quota_bytes=None,
    )


async def test_an_update_adds_this_conversations_files(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    cid = await talk(spark_db)
    await conversation_members.share(spark_db, ADA, cid, CORA, Role.CONTRIBUTOR)
    mine = await _file(spark_db, ADA, cid, "notes.txt")
    coras = await _file(spark_db, CORA, cid, "cora.txt")
    elsewhere = await _file(spark_db, ADA, None, "other.txt")
    card = await cards.propose_update(spark_db, ADA, cid, top, add_file_ids=[mine])
    assert card.payload["files"] == [{"file_id": mine, "name": "notes.txt"}]
    assert await projects_db.list_resources(spark_db, ADA, top) == []
    await cards.accept(spark_db, ADA, card.id)
    assert [r["file_id"] for r in await projects_db.list_resources(spark_db, ADA, top)] == [mine]
    # Only the user's own files in this conversation.
    for fid in (coras, elsewhere, 999_999):
        with pytest.raises(ProjectRefused):
            await cards.propose_update(spark_db, ADA, cid, top, add_file_ids=[fid])
    # Already a resource.
    with pytest.raises(ProjectRefused):
        await cards.propose_update(spark_db, ADA, cid, top, add_file_ids=[mine])


# -- Moves -----------------------------------------------------------------------------


async def test_a_move_shows_who_would_see_the_conversation(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    cid = await talk(spark_db)
    card = await cards.propose_move(spark_db, ADA, cid, top)
    assert card.kind is CardKind.MOVE
    assert card.payload["widened"] == {CORA: "contributor", VIC: "viewer"}
    assert card.payload["to"] == {"id": top, "name": "Plan"}
    row = await spark_db.fetch_one("SELECT project_id FROM conversations WHERE id = ?", (cid,))
    assert row is not None and row["project_id"] is None  # nothing moved yet
    await cards.accept(spark_db, ADA, card.id)
    row = await spark_db.fetch_one("SELECT project_id FROM conversations WHERE id = ?", (cid,))
    assert row is not None and row["project_id"] == top


async def test_a_move_whose_report_changed_is_shown_again(spark_db: Database) -> None:
    top = await projects_db.create_project(spark_db, ADA, "Quiet")
    cid = await talk(spark_db)
    card = await cards.propose_move(spark_db, ADA, cid, top)
    assert card.payload["widened"] == {}
    # Cora joins the project before Ada confirms: the move would now show her the talk.
    await members_db.share(
        spark_db, ADA, top, CORA, ProjectRole.VIEWER, ShareScope.ALL_CONVERSATIONS
    )
    with pytest.raises(MoveChanged) as changed:
        await cards.accept(spark_db, ADA, card.id)
    assert changed.value.card.payload["widened"] == {CORA: "viewer"}
    assert changed.value.card.status is CardStatus.PENDING
    row = await spark_db.fetch_one("SELECT project_id FROM conversations WHERE id = ?", (cid,))
    assert row is not None and row["project_id"] is None
    await cards.accept(spark_db, ADA, card.id)
    row = await spark_db.fetch_one("SELECT project_id FROM conversations WHERE id = ?", (cid,))
    assert row is not None and row["project_id"] == top


async def test_a_move_the_projects_rules_refuse(spark_db: Database) -> None:
    top, _ = await plan(spark_db)
    other = await projects_db.create_project(spark_db, CORA, "Cora's")
    cid = await talk(spark_db)
    with pytest.raises(NotOwned):  # a project Ada cannot reach
        await cards.propose_move(spark_db, ADA, cid, other)
    coras = await talk(spark_db, CORA)
    with pytest.raises(NotPermitted):  # a project Cora contributes to but does not own
        await cards.propose_move(spark_db, CORA, coras, top)
    with pytest.raises((NotOwned, NotPermitted)):  # a conversation Ada did not start
        await cards.propose_move(spark_db, ADA, coras, top)
    assert not await spark_db.fetch_value("SELECT COUNT(*) FROM project_cards")


async def test_a_card_line_carries_only_its_id(spark_db: Database) -> None:
    cid = await talk(spark_db)
    card = await cards.suggest(
        spark_db,
        ADA,
        cid,
        name="G" * 150,
        description="d" * 900,
        instructions="i" * 4000,
        reason="",
    )
    line = card.line()
    assert line.startswith(cards.CARD_MARKER + " ")
    data = json.loads(line[len(cards.CARD_MARKER) :])
    assert data == {"card": card.id, "conversation_id": cid, "kind": "suggestion"}
    assert len(line) < 200
    assert cards.parse_line(f"before\n{line}\nafter") == data
    assert cards.parse_line("nothing here") is None
