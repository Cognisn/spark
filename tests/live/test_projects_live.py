"""Projects in server mode on the live stack (wave 32, kick-off scope 1; PC6 to PC18).

Four people, invited through the administration screens and signed in on web: the
owner, a Contributor, a Read-only member and a non-member. The owner builds a
three-level project with instructions at each level and shares its top level with
the Contributor (project and all conversations) and the Read-only member (project
only). Every check goes through the HTTP API the screens use; what the model was
sent is read from the model stub's record, and the database is read only to observe
(whose area a file is charged to, notices). Users and data are synthetic.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from tests.live.conftest import Person, Stack, invite_and_accept, wait_for
from tests.live.live_helpers import (
    add_resource,
    can_read,
    chats_since,
    create_project,
    instance,
    new_conversation,
    ok,
    preview_card,
    project_card,
    role_in,
    tool_turn,
    turn,
    upload,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]

NEAREST = "a deeper level refines or overrides a higher one"
#: The defect the quota case waits on (Skuld question, wave 32).
QUOTA_QUESTION = (
    "Skuld question 93: in server mode the file area reads files.quota_mb from the "
    "start-up settings, not the administrator's instance setting"
)


@dataclass
class Team:
    owner: Person
    contributor: Person
    viewer: Person
    outsider: Person


@dataclass
class Tree:
    top: int
    middle: int
    bottom: int
    marks: tuple[str, str, str]


@pytest.fixture(scope="module")
def team(stack: Stack, admin: Person, configured: None) -> Iterator[Team]:
    people = [
        invite_and_accept(stack, admin, f"{who}-{stack.run}@example.com", "user")
        for who in ("olivia", "colin", "vera", "nora")
    ]
    yield Team(*people)
    for person in people:
        person.browser.close()


def share(owner: Person, project_id: int, person: Person, role: str, scope: str) -> str:
    body = {"user_id": person.warden_id, "role": role, "scope": scope}
    found = ok(owner.browser.post(f"/projects/api/{project_id}/members", json=body))
    return str(found["status"])


@pytest.fixture(scope="module")
def tree(stack: Stack, team: Team) -> Tree:
    marks = (f"TOPMARK{stack.run}", f"MIDMARK{stack.run}", f"LOWMARK{stack.run}")
    owner = team.owner.browser
    top = create_project(
        owner,
        f"Garden {stack.run}",
        instructions=f"{marks[0]}: write in a formal tone.",
        description="The garden at example.org.",
    )
    middle = create_project(
        owner, "Beds", parent_id=top, instructions=f"{marks[1]}: write in a casual tone."
    )
    bottom = create_project(
        owner, "Herbs", parent_id=middle, instructions=f"{marks[2]}: answer in one line."
    )
    assert share(team.owner, top, team.contributor, "contributor", "all_conversations") == "added"
    assert share(team.owner, top, team.viewer, "viewer", "project_only") == "added"
    return Tree(top, middle, bottom, marks)


# -- inheritance and nearest wins ---------------------------------------------------------------


def test_every_level_reaches_the_prompt_top_first_and_the_conversation_last(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC7, PC8: the instructions of each level, top-level first, each labelled with its
    project, then the conversation's own; the model is told the nearest level wins."""
    own = f"CONVMARK{stack.run}"
    conv = new_conversation(
        team.owner, model_id, "Inherits", project_id=tree.bottom, instructions=own
    )
    since = time.time()
    turn(team.owner, conv, f"inherit {stack.run} [[tokens 2]]")
    sent = [r for r in chats_since(stack, since) if stack.run in r["user"]]
    assert sent, "the turn never reached the model"
    system = sent[0]["system"]
    assert "## Project instructions" in system, system[-3000:]
    assert NEAREST in system, system[-3000:]
    places = [system.find(mark) for mark in (*tree.marks, own)]
    assert all(p >= 0 for p in places), (places, system[-3000:])
    assert places == sorted(places), f"levels out of order: {places}"
    for level, name in ((1, f"Garden {stack.run}"), (2, "Beds"), (3, "Herbs")):
        assert f'### Level {level}: project "{name}"' in system, system[-3000:]

    middle_conv = new_conversation(team.owner, model_id, "Middle", project_id=tree.middle)
    since = time.time()
    turn(team.owner, middle_conv, f"middle {stack.run} [[tokens 2]]")
    sent = [r for r in chats_since(stack, since) if f"middle {stack.run}" in r["user"]]
    assert tree.marks[0] in sent[0]["system"] and tree.marks[1] in sent[0]["system"]
    assert tree.marks[2] not in sent[0]["system"], "a deeper level reached a higher one"


def test_a_contributors_resource_is_charged_to_the_owner_and_read_on_demand(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC10, PC11: a Contributor adds a resource; it is kept in the owner's area and
    counts against the owner's quota; it is listed in the prompt of a conversation below
    and read with read_my_file; the Read-only member may not add one; a Contributor
    removes only what they added."""
    text = f"Sow basil after the last frost. RESOURCEMARK{stack.run}\n".encode()
    before = stack.sql(
        "SELECT coalesce(sum(size_bytes), 0) FROM user_files WHERE user_guid = %s",
        (team.owner.guid,),
    )[0][0]
    file_id = add_resource(team.contributor.browser, tree.middle, "basil-notes.txt", text)
    charged = stack.sql("SELECT user_guid, size_bytes FROM user_files WHERE id = %s", (file_id,))
    assert charged == [(team.owner.guid, len(text))], charged
    after = stack.sql(
        "SELECT coalesce(sum(size_bytes), 0) FROM user_files WHERE user_guid = %s",
        (team.owner.guid,),
    )[0][0]
    assert after - before == len(text)
    added_by = stack.sql("SELECT added_by FROM project_resources WHERE file_id = %s", (file_id,))
    assert added_by == [(team.contributor.guid,)], added_by

    refused = team.viewer.browser.post(
        f"/projects/api/{tree.middle}/resources",
        files={"file": ("viewer.txt", b"not allowed\n", "text/plain")},
    )
    assert refused.status_code == 403, refused.text
    assert team.outsider.browser.get(f"/projects/api/{tree.middle}").status_code == 404

    conv = new_conversation(team.contributor, model_id, "Reads basil", project_id=tree.bottom)
    _events, first, result = tool_turn(
        stack, team.contributor, conv, "read the notes", "read_my_file", {"file_id": file_id}
    )
    assert "### Project resources" in first["system"], first["system"][-3000:]
    assert "basil-notes.txt" in first["system"], first["system"][-3000:]
    assert f"RESOURCEMARK{stack.run}" in result, result[:2000]

    owners = add_resource(team.owner.browser, tree.middle, "owner-notes.txt", b"owner's\n")
    gone = team.contributor.browser.delete(f"/projects/api/{tree.middle}/resources/{owners}")
    assert gone.status_code == 403, gone.text
    mine = team.contributor.browser.delete(f"/projects/api/{tree.middle}/resources/{file_id}")
    assert mine.status_code == 200, mine.text
    assert not stack.sql("SELECT id FROM user_files WHERE id = %s", (file_id,))


# -- sharing scopes -------------------------------------------------------------------------


def test_each_sharing_scope_decides_what_each_person_sees(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC9, PC10: under "project and all conversations" the Contributor sees every
    conversation in the tree at their role; under "project only" the Read-only member
    sees the project but none of its conversations; the non-member sees nothing. The
    Read-only member cannot start a conversation; a Contributor can, anywhere in the
    tree, and the owner sees it."""
    conv = new_conversation(team.owner, model_id, "Owner's herbs", project_id=tree.bottom)
    assert role_in(team.contributor, conv) == "contributor"
    assert not can_read(team.viewer, conv)
    assert not can_read(team.outsider, conv)

    page = ok(team.viewer.browser.get(f"/projects/api/{tree.top}"))
    assert page["project"]["role"] == "viewer", page["project"]
    assert page["members"] is None, "a member was shown the member list"
    assert conv not in [c["id"] for c in page["conversations"]]
    refused = team.viewer.browser.post(
        "/conversations/api/create",
        json={"name": "Viewer's", "model_id": model_id, "project_id": tree.middle},
    )
    assert refused.status_code == 403, refused.text
    assert team.outsider.browser.get(f"/projects/api/{tree.top}").status_code == 404

    started = new_conversation(team.contributor, model_id, "Colin's beds", project_id=tree.middle)
    assert can_read(team.owner, started)
    assert not can_read(team.viewer, started)
    listed = ok(team.contributor.browser.get("/projects/api"))
    assert tree.top in [p["id"] for p in listed["shared"]], listed
    assert tree.top not in [p["id"] for p in listed["owned"]]


def test_a_scope_change_hides_the_projects_conversations_but_not_an_individual_share(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC9: widening the Read-only member to "project and all conversations" shows them
    the project's conversations; narrowing back to "project only" returns them to
    private, except the one shared with them individually."""
    shared = new_conversation(team.owner, model_id, "Shared alone", project_id=tree.top)
    other = new_conversation(team.owner, model_id, "Not shared", project_id=tree.middle)
    assert share(team.owner, tree.top, team.viewer, "viewer", "all_conversations") == "changed"
    try:
        assert role_in(team.viewer, shared) == "viewer"
        assert role_in(team.viewer, other) == "viewer"
        body = {"user_id": team.viewer.warden_id, "role": "viewer"}
        ok(team.owner.browser.post(f"/conversations/api/{shared}/members", json=body))
    finally:
        assert share(team.owner, tree.top, team.viewer, "viewer", "project_only") == "changed"
    assert role_in(team.viewer, shared) == "viewer", "the individual share was lost"
    assert not can_read(team.viewer, other), "a project conversation stayed visible"


def test_a_conversation_in_a_project_is_shared_only_with_its_people(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC9 (the share picker limit): a conversation in a project can be shared only with
    people who have access to the project; the screen's limited picker leans on this."""
    conv = new_conversation(team.owner, model_id, "Picker", project_id=tree.middle)
    outside = team.owner.browser.post(
        f"/conversations/api/{conv}/members",
        json={"user_id": team.outsider.warden_id, "role": "viewer"},
    )
    assert outside.status_code == 400, outside.text
    assert "access to the project" in outside.json()["error"], outside.text
    assert not can_read(team.outsider, conv)
    inside = team.owner.browser.post(
        f"/conversations/api/{conv}/members",
        json={"user_id": team.viewer.warden_id, "role": "viewer"},
    )
    assert inside.status_code == 200 and inside.json()["status"] == "added", inside.text
    assert can_read(team.viewer, conv)


# -- moves ----------------------------------------------------------------------------------


def test_moves_warn_before_widening_or_narrowing_and_follow_the_rules(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC12: a move that would show the conversation to more people, or hide it from
    some, answers 409 with the report and moves nothing until confirmed; a move between
    two projects goes through "no project"; only the owner of both may move."""
    conv = new_conversation(team.owner, model_id, "Loose")
    path = f"/projects/api/conversations/{conv}/move"

    held = team.owner.browser.post(path, json={"project_id": tree.top})
    assert held.status_code == 409, held.text
    report = held.json()
    assert team.contributor.guid in report["widened"], report
    assert report["confirm_required"] is True
    assert not can_read(team.contributor, conv), "an unconfirmed move took effect"
    moved = ok(team.owner.browser.post(path, json={"project_id": tree.top, "confirm": True}))
    assert moved["to_project"] == tree.top, moved
    assert role_in(team.contributor, conv) == "contributor"

    inside = ok(team.owner.browser.post(path, json={"project_id": tree.bottom}))
    assert inside["to_project"] == tree.bottom, "a move within the tree needed confirming"

    other = create_project(team.owner.browser, f"Orchard {stack.run}")
    across = team.owner.browser.post(path, json={"project_id": other, "confirm": True})
    assert across.status_code == 400, across.text

    out = team.owner.browser.post(path, json={"project_id": None})
    assert out.status_code == 409, out.text
    assert team.contributor.guid in out.json()["narrowed"], out.json()
    ok(team.owner.browser.post(path, json={"project_id": None, "confirm": True}))
    assert not can_read(team.contributor, conv)

    theirs = new_conversation(team.contributor, model_id, "Colin's own")
    stolen = team.contributor.browser.post(
        f"/projects/api/conversations/{theirs}/move", json={"project_id": tree.top}
    )
    assert stolen.status_code in (403, 404), stolen.text
    taken = team.contributor.browser.post(path, json={"project_id": other, "confirm": True})
    assert taken.status_code in (403, 404), taken.text


# -- deletion and transfer ------------------------------------------------------------------


def _notices(stack: Stack, person: Person, project_id: int) -> list[tuple[Any, ...]]:
    return stack.sql(
        "SELECT kind, count FROM project_notices WHERE user_guid = %s AND project_id = %s",
        (person.guid, project_id),
    )


def test_deleting_keeps_or_deletes_conversations_with_counts_and_notices(
    stack: Stack, team: Team, model_id: str
) -> None:
    """PC13: "keep" moves a sub-project's conversations up to its parent and a top-level
    project's to no project, private to whoever started each; "everything" deletes
    other members' conversations too and tells them. Resources go either way."""
    owner, colin = team.owner, team.contributor
    top = create_project(owner.browser, f"Shed {stack.run}")
    sub = create_project(owner.browser, "Tools", parent_id=top)
    share(owner, top, colin, "contributor", "all_conversations")
    colins = new_conversation(colin, model_id, "Colin's tools", project_id=sub)
    owners = new_conversation(owner, model_id, "Owner's tools", project_id=sub)
    resource = add_resource(owner.browser, sub, "tools.txt", b"spade, fork\n")

    preview = ok(owner.browser.get(f"/projects/api/{sub}/delete-preview"))
    assert preview["conversations"] == 2 and preview["conversations_by_others"] == 1, preview
    assert preview["resources"] == 1, preview
    kept = ok(owner.browser.post(f"/projects/api/{sub}/delete", json={"mode": "keep"}))
    assert kept["conversations_kept"] == 2, kept
    rows = stack.sql(
        "SELECT id, project_id FROM conversations WHERE id IN (%s, %s)", (colins, owners)
    )
    assert {r[1] for r in rows} == {top}, rows
    assert not stack.sql("SELECT id FROM user_files WHERE id = %s", (resource,))
    assert owner.browser.get(f"/projects/api/{sub}").status_code == 404

    preview = ok(owner.browser.get(f"/projects/api/{top}/delete-preview"))
    assert preview["members"] == 1 and preview["people_told"] == 1, preview
    sent_at = time.time()
    gone = ok(owner.browser.post(f"/projects/api/{top}/delete", json={"mode": "everything"}))
    assert gone["conversations_deleted"] == 2 and gone["people_told"] == 1, gone
    # Deleted as SPARK deletes any conversation (soft): inactive, its turns gone, and in
    # nobody's list; nobody but its starter reaches it by id.
    active = stack.sql("SELECT is_active FROM conversations WHERE id IN (%s, %s)", (colins, owners))
    assert [bool(a) for (a,) in active] == [False, False], active
    for person, conv in ((colin, colins), (owner, owners)):
        listed = ok(person.browser.get("/conversations/api/list"))
        rows = listed.get("conversations", listed) if isinstance(listed, dict) else listed
        assert conv not in [int(c["id"]) for c in rows], f"{conv} still listed"
    assert not can_read(owner, colins), "the owner still reaches another's deleted conversation"
    assert [k for k, _ in _notices(stack, colin, top)] == ["project_deleted"], _notices(
        stack, colin, top
    )
    seen = ok(colin.browser.get("/projects/api/notices"))["notices"]
    assert any(n["project_id"] == top for n in seen), seen
    assert _mail_mentions(stack, colin.email, f"Shed {stack.run}", sent_at)

    kept_top = create_project(owner.browser, f"Loft {stack.run}")
    share(owner, kept_top, colin, "contributor", "all_conversations")
    loft = new_conversation(colin, model_id, "Colin's loft", project_id=kept_top)
    assert can_read(owner, loft)
    ok(owner.browser.post(f"/projects/api/{kept_top}/delete", json={"mode": "keep"}))
    row = stack.sql("SELECT project_id, user_guid FROM conversations WHERE id = %s", (loft,))
    assert row == [(None, colin.guid)], row
    assert not can_read(owner, loft), "a kept conversation stayed shared through the project"
    assert can_read(colin, loft)


def _mail_mentions(stack: Stack, to: str, text: str, after: float) -> bool:
    import httpx

    with httpx.Client(base_url=stack.state["mail_api"], timeout=10) as api:

        def found() -> bool:
            listed = api.get("/api/v1/search", params={"query": f'to:"{to}"'}).json()
            for summary in listed.get("messages") or []:
                message = api.get(f"/api/v1/message/{summary['ID']}").json()
                body = (message.get("Text") or "") + (message.get("Subject") or "")
                if text in body:
                    return True
            return False

        return bool(wait_for(found, 60, f"a mail to {to} naming {text}"))


def test_transfer_moves_the_project_and_its_files_to_the_new_owner(
    stack: Stack, team: Team, model_id: str
) -> None:
    """PC14: the new owner takes the project and its resources with their usage; the old
    owner stays as a Contributor with all conversations; conversations stay with whoever
    started them; the new owner is told."""
    owner, colin = team.owner, team.contributor
    project = create_project(owner.browser, f"Pond {stack.run}")
    share(owner, project, colin, "contributor", "all_conversations")
    add_resource(owner.browser, project, "pond.txt", b"p" * 4_000)
    started = new_conversation(owner, model_id, "Owner's pond", project_id=project)
    used = _usage(stack, colin)

    done = ok(
        owner.browser.post(f"/projects/api/{project}/transfer", json={"user_guid": colin.guid})
    )
    assert done["status"] == "transferred" and done["files"] == 1, done
    assert stack.sql("SELECT user_guid FROM projects WHERE id = %s", (project,)) == [(colin.guid,)]
    moved = stack.sql(
        "SELECT f.user_guid FROM project_resources r JOIN user_files f ON f.id = r.file_id "
        "WHERE r.project_id = %s",
        (project,),
    )
    assert moved == [(colin.guid,)], moved
    assert _usage(stack, colin) - used == 4_000, "the usage did not move with the files"
    page = ok(owner.browser.get(f"/projects/api/{project}"))["project"]
    assert (page["role"], page["scope"]) == ("contributor", "all_conversations"), page
    assert stack.sql("SELECT user_guid FROM conversations WHERE id = %s", (started,)) == [
        (owner.guid,)
    ]
    kinds = {k for k, _ in _notices(stack, colin, project)}
    assert "project_transferred" in kinds, kinds


def _usage(stack: Stack, person: Person) -> int:
    found = stack.sql("SELECT bytes_used FROM user_file_usage WHERE user_guid = %s", (person.guid,))
    return int(found[0][0]) if found else 0


@pytest.mark.xfail(strict=True, reason=QUOTA_QUESTION)
def test_transfer_is_refused_when_the_new_owner_lacks_the_quota(
    stack: Stack, admin: Person, team: Team
) -> None:
    """PC14: with the administrator's quota (files.quota_mb) at 1 MB, a transfer to someone
    whose area already holds 700 kB refuses a project of 400 kB (507) and moves nothing.

    Fails on 0.3.0rc65: the quota an administrator sets on the instance is not what the
    file area reads (see the reason)."""
    owner, colin = team.owner, team.contributor
    project = create_project(owner.browser, f"Well {stack.run}")
    share(owner, project, colin, "contributor", "all_conversations")
    add_resource(owner.browser, project, "well.bin", b"w" * 400_000)
    previous = stack.sql("SELECT value FROM instance_settings WHERE key = 'files.quota_mb'")
    instance(admin, {"files.quota_mb": 1})
    filler = None
    try:
        filler = upload(colin.browser, "filler.bin", b"f" * 700_000)
        full = owner.browser.post(
            f"/projects/api/{project}/transfer", json={"user_guid": colin.guid}
        )
        assert full.status_code == 507, (full.status_code, full.text)
        assert stack.sql("SELECT user_guid FROM projects WHERE id = %s", (project,)) == [
            (owner.guid,)
        ]
    finally:
        if filler is not None:
            colin.browser.delete(f"/files/api/{filler}")
        restored = int(str(previous[0][0]).strip('"')) if previous else 1024
        instance(admin, {"files.quota_mb": restored})


# -- the right-hand panel -----------------------------------------------------------------


BAR = {
    "type": "bar",
    "data": {"columns": ["quarter", "units"], "rows": [["Q1", 12], ["Q2", 18], ["Q3", 9]]},
    "encoding": {"x": "quarter", "y": "units"},
}


def test_the_panel_lists_three_groups_and_renames_by_role(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC2 to PC5: Generated or retrieved (a chart), Provided (each attachment labelled
    with who attached it, in a shared conversation) and Project resources by level; the
    owner and a Contributor rename, a Read-only member may not, a stranger finds nothing."""
    owner, colin = team.owner, team.contributor
    add_resource(owner.browser, tree.top, "top.txt", b"top level\n")
    add_resource(owner.browser, tree.bottom, "bottom.txt", b"bottom level\n")
    conv = new_conversation(owner, model_id, "Panel", project_id=tree.bottom)
    mine = upload(owner.browser, "owners-list.txt", b"owner's list\n", conversation_id=conv)
    theirs = upload(colin.browser, "colins-list.txt", b"colin's list\n", conversation_id=conv)
    _e, _first, result = tool_turn(
        stack,
        owner,
        conv,
        "chart it",
        "create_chart",
        {"title": "Units by quarter", "definition": BAR, "caption": "Three quarters."},
    )
    chart = preview_card(result)
    assert chart["kind"] == "chart" and chart["title"] == "Units by quarter", chart

    panel = ok(owner.browser.get(f"/artefacts/api/{conv}"))
    assert panel["shared"] is True and panel["can_rename"] is True, panel
    assert [a["file_id"] for a in panel["generated"]] == [chart["file_id"]], panel["generated"]
    provided = {a["file_id"]: a for a in panel["provided"]}
    assert set(provided) == {mine, theirs}, panel["provided"]
    assert provided[mine]["provided_by"]["is_me"] is True
    assert provided[theirs]["provided_by"]["is_me"] is False
    assert provided[theirs]["provided_by"]["name"], provided[theirs]
    levels = {g["level"]: [i["name"] for i in g["items"]] for g in panel["project"]}
    assert "top.txt" in levels[1] and "bottom.txt" in levels[3], panel["project"]
    assert panel["generated"][0]["message_id"], "the chart is not tied to its message"

    target = f"/artefacts/api/{chart['file_id']}"
    assert ok(owner.browser.request("PATCH", target, json={"title": "Units, Q1 to Q3"}))
    assert ok(colin.browser.request("PATCH", target, json={"title": "Units per quarter"}))
    ok(
        owner.browser.post(
            f"/conversations/api/{conv}/members",
            json={"user_id": team.viewer.warden_id, "role": "viewer"},
        )
    )
    viewed = ok(team.viewer.browser.get(f"/artefacts/api/{conv}"))
    assert viewed["can_rename"] is False, viewed
    refused = team.viewer.browser.request("PATCH", target, json={"title": "Viewer's title"})
    assert refused.status_code == 403, refused.text
    stranger = team.outsider.browser.request("PATCH", target, json={"title": "Stranger's"})
    assert stranger.status_code == 404, stranger.text
    assert team.outsider.browser.get(f"/artefacts/api/{conv}").status_code == 404
    title = stack.sql("SELECT title FROM user_files WHERE id = %s", (chart["file_id"],))
    assert title == [("Units per quarter",)], title
    renamed = stack.sql(
        "SELECT count(*) FROM warden.audit_events WHERE action = 'spark.artefact.renamed' "
        "AND resource_id = %s",
        (str(chart["file_id"]),),
    )
    assert renamed[0][0] == 2, renamed


# -- the project tools --------------------------------------------------------------------


def test_create_project_on_request_creates_it_and_refuses_without_one(
    stack: Stack, team: Team, model_id: str
) -> None:
    """PC17: when the user's message asks for a project, create_project makes it at
    once; when it does not, nothing is created whatever the model claims."""
    conv = new_conversation(team.owner, model_id, "Tool: create")
    name = f"Allotment {stack.run}"
    args = {
        "name": name,
        "description": "Plot 12 at example.org.",
        "instructions": "Keep a planting log.",
        "requested_by_user": True,
    }
    _e, _first, result = tool_turn(
        stack, team.owner, conv, "Please create a project for my allotment.", "create_project", args
    )
    created = re.search(r"created as project (\d+)", result)
    assert created, result[:2000]
    project = ok(team.owner.browser.get(f"/projects/api/{created.group(1)}"))["project"]
    assert project["name"] == name and project["role"] == "owner", project
    assert project["instructions"] == "Keep a planting log.", project
    assert stack.sql("SELECT project_id FROM conversations WHERE id = %s", (conv,)) == [(None,)]

    other = dict(args, name=f"Unasked {stack.run}")
    _e, _first, refused = tool_turn(
        stack, team.owner, conv, "Here are this week's notes.", "create_project", other
    )
    assert "Nothing was created" in refused, refused[:2000]
    assert not stack.sql("SELECT id FROM projects WHERE name = %s", (other["name"],))


def test_a_suggestion_is_a_card_dismissed_once_and_never_shown_again(
    stack: Stack, team: Team, model_id: str
) -> None:
    """PC17: suggest_project shows a card and creates nothing; after Dismiss, a second
    suggestion in the conversation is refused; other people cannot act on the card."""
    conv = new_conversation(team.owner, model_id, "Tool: suggest")
    name = f"Suggested {stack.run}"
    args = {"name": name, "description": "Recurring notes.", "reason": "You repeat yourself."}
    _e, _first, result = tool_turn(
        stack, team.owner, conv, "Weekly notes again.", "suggest_project", args
    )
    card = project_card(result)
    assert card["kind"] == "suggestion" and card["conversation_id"] == conv, card
    assert not stack.sql("SELECT id FROM projects WHERE name = %s", (name,))
    shown = ok(team.owner.browser.get(f"/project-cards/api/{card['card']}"))
    assert shown["card"]["can_act"] is True, shown
    assert team.outsider.browser.get(f"/project-cards/api/{card['card']}").status_code == 404
    stranger = team.outsider.browser.post(f"/project-cards/api/{card['card']}/accept", json={})
    assert stranger.status_code == 404, stranger.text

    ok(team.owner.browser.post(f"/project-cards/api/{card['card']}/dismiss", json={}))
    again = team.owner.browser.post(f"/project-cards/api/{card['card']}/accept", json={})
    assert again.status_code == 409, again.text
    _e, _first, second = tool_turn(
        stack, team.owner, conv, "More notes.", "suggest_project", dict(args, name="Second")
    )
    assert "No suggestion was shown" in second, second[:2000]
    assert "[spark-project-card]" not in second
    assert not stack.sql("SELECT id FROM projects WHERE name IN (%s, 'Second')", (name,))


def test_an_update_waits_for_the_owner_to_confirm_its_card(
    stack: Stack, team: Team, tree: Tree, model_id: str
) -> None:
    """PC18: update_project shows a before-and-after card and changes nothing until the
    owner accepts it; a file of the conversation becomes a resource on acceptance."""
    conv = new_conversation(team.owner, model_id, "Tool: update")
    attached = upload(team.owner.browser, "plan.txt", b"the plan\n", conversation_id=conv)
    project = create_project(team.owner.browser, f"Greenhouse {stack.run}", instructions="Old.")
    args = {
        "project_id": project,
        "changes": {"instructions": "New: water at dawn.", "add_file_ids": [attached]},
    }
    _e, _first, result = tool_turn(
        stack, team.owner, conv, "Please update the greenhouse project.", "update_project", args
    )
    card = project_card(result)
    assert card["kind"] == "update", card
    page = ok(team.owner.browser.get(f"/projects/api/{project}"))
    assert page["project"]["instructions"] == "Old." and not page["resources"], page
    shown = ok(team.owner.browser.get(f"/project-cards/api/{card['card']}"))
    assert "New: water at dawn." in str(shown), shown

    accepted = ok(team.owner.browser.post(f"/project-cards/api/{card['card']}/accept", json={}))
    assert accepted["project_id"] == project, accepted
    page = ok(team.owner.browser.get(f"/projects/api/{project}"))
    assert page["project"]["instructions"] == "New: water at dawn.", page["project"]
    assert [r["file_id"] for r in page["resources"]] == [attached], page["resources"]
    used = team.owner.browser.post(f"/project-cards/api/{card['card']}/accept", json={})
    assert used.status_code == 409, used.text
