"""The project page and the chat header's hooks render per user (PC16, PC9 to PC14).

Personal mode (SQLite): the page for the owner, a missing project as 404, the sidebar tree
host on the Conversations page. Server mode (PostgreSQL, two or more users): the page
shell for every role that may see the project, 404 for a stranger, and the API payloads the
screens draw from give each role exactly what it is offered (members for the owner only,
the capability flags the sections follow).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_projects_personal import personal  # noqa: F401
from tests.test_projects_web import (  # noqa: F401
    FETCH,
    client,
    host,
    mail,
    people,
    plan,
    script,
    server_db,
    storage,
)

PAGE = {"Accept": "text/html"}


def test_personal_mode_renders_the_page_and_the_tree_host(
    personal: TestClient,
) -> None:  # noqa: F811
    made = personal.post("/projects/api", json={"name": "Home <b>", "description": "A place."})
    pid = made.json()["project"]["id"]
    page = personal.get(f"/projects/{pid}", headers=PAGE)
    assert page.status_code == 200
    text = page.text
    assert 'id="project-page"' in text and f'data-project-id="{pid}"' in text
    assert 'data-server="0"' in text
    assert "Home &lt;b&gt;" in text and "Home <b>" not in text
    for section in (
        "instructions-heading",
        "resources-heading",
        "subprojects-heading",
        "conversations-heading",
    ):
        assert section in text
    assert "/static/js/projects.js" in text and "/static/css/projects.css" in text
    assert personal.get("/projects/999999", headers=PAGE).status_code == 404

    convs = personal.get("/conversations", headers=PAGE).text
    assert 'id="projects-tree"' in convs and "New project" in convs
    assert "/static/js/projects.js" in convs and 'id="newProjectModal"' in convs


@pytest.mark.postgres
class TestServerPages:
    def test_each_role_gets_the_page_and_a_stranger_does_not(
        self, people: dict[str, Any], plan: dict[str, int]  # noqa: F811
    ) -> None:
        top = plan["plan"]
        for who in ("ada", "cora", "vic", "pat"):
            got = people[who].browser.get(f"/projects/{top}", headers=PAGE)
            assert got.status_code == 200, who
            assert 'data-server="1"' in got.text and "members-section" in got.text
        assert people["sam"].browser.get(f"/projects/{top}", headers=PAGE).status_code == 404
        # A sub-project is reached through its top-level project's members.
        assert (
            people["vic"].browser.get(f"/projects/{plan['detail']}", headers=PAGE).status_code
            == 200
        )

    def test_the_payloads_follow_the_role(
        self, people: dict[str, Any], plan: dict[str, int]  # noqa: F811
    ) -> None:
        def view(who: str) -> dict[str, Any]:
            got = people[who].browser.get(f"/projects/api/{plan['plan']}", headers=FETCH)
            assert got.status_code == 200
            body: dict[str, Any] = got.json()
            return body

        ada, cora, vic = view("ada"), view("cora"), view("vic")
        assert ada["project"]["can_manage"] is True and len(ada["members"]) == 3
        assert cora["project"]["can_contribute"] is True and cora["project"]["can_manage"] is False
        assert cora["members"] is None
        assert vic["project"]["can_contribute"] is False and vic["members"] is None

    def test_the_tree_lists_owned_and_shared_with_the_conversations_a_person_sees(
        self, people: dict[str, Any], plan: dict[str, int], storage: dict[str, Any]  # noqa: F811
    ) -> None:
        cid = people["cora"].browser.post(
            "/conversations/api/create",
            json={"name": "Cora talk", "model_id": "stub/model-a", "project_id": plan["plan"]},
        )
        assert cid.status_code in (200, 201), cid.text
        tree = people["vic"].browser.get("/projects/api", headers=FETCH).json()
        assert tree["owned"] == [] and [n["name"] for n in tree["shared"]] == ["Plan"]
        assert tree["shared"][0]["owner_name"]
        assert [c["name"] for c in tree["shared"][0]["conversations"]] == ["Cora talk"]
        # Pat (project only) sees the project, not Cora's conversation.
        pat = people["pat"].browser.get("/projects/api", headers=FETCH).json()
        assert pat["shared"][0]["conversations"] == []
        # The chat page's hooks: the crumbs bar and the Move button for its owner.
        chat = people["cora"].browser.get(f"/chat/{cid.json()['id']}", headers=PAGE)
        assert chat.status_code == 200
        assert 'id="project-crumbs-chat"' in chat.text and 'id="btn-move-conversation"' in chat.text
        assert 'id="convMoveModal"' in chat.text
        ctx = (
            people["cora"]
            .browser.get(f"/projects/api/conversations/{cid.json()['id']}/context", headers=FETCH)
            .json()
        )
        assert [p["name"] for p in ctx["path"]] == ["Plan"]


def test_no_template_names_an_internal_path() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
    for name in ("project.html", "_project_members_dialog.html", "_new_project_dialog.html"):
        text = (root / "templates" / name).read_text(encoding="utf-8")
        assert "C:\\" not in text and "D:\\" not in text
