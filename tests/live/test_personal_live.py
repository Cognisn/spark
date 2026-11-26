"""Personal mode on this host, and its bundle in a server account (wave 32, kick-off scope
3 and the bundle part of scope 1; PC15, PC19 to PC21).

A personal-mode SPARK runs from this repository with its folders in the sandbox and an
Ollama connection to the stack's model stub on its published port. Projects work
there without sharing, members or transfers; charts, the panel and the built-in
skills work as in server mode. Its user is then exported with the real ``spark
export`` (bundle format version 2) and imported into a server account through the
administration screen: the projects arrive owned by that user and shared with nobody.
Users and data are synthetic.
"""

from __future__ import annotations

import json
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from tests.live.client import Browser, named
from tests.live.conftest import Person, Stack, invite_and_accept, wait_for
from tests.live.live_helpers import (
    add_resource,
    call64,
    chats_since,
    create_project,
    offers_skill,
    ok,
    preview_card,
    upload,
)
from tests.live.personal_app import PersonalApp, export_bundle

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1800)]

MODEL = "ollama/llama3-stub"
BUILT_IN = ("charts", "mermaid-diagrams", "projects")


@dataclass
class Personal:
    app: PersonalApp
    browser: Browser
    top: int
    sub: int
    conversation: int


def _chat(
    stack: Stack, browser: Browser, conv: int, text: str
) -> tuple[dict[str, Any], str | None]:
    """A turn on the personal app: the first request the stub received and the tool
    result passed back, if the model called a tool."""
    marker = f"personal-{time.time_ns()}"
    since = time.time()
    events = browser.chat(conv, f"{text} {marker}", approve="once")
    assert named(events, "complete"), [(e.name, e.data) for e in events][-5:]
    sent = [r for r in chats_since(stack, since) if marker in r["user"]]
    assert sent, "the personal turn never reached the stub"
    follow = [r for r in sent if r["last_role"] == "tool"]
    return sent[0], (str(follow[0]["last"]) if follow else None)


@pytest.fixture(scope="module")
def personal(stack: Stack) -> Iterator[Personal]:
    home = stack.sandbox / "personal" / f"projects-{stack.run}"
    app = PersonalApp(
        home, int(stack.state["ports"]["web"]) + 50, int(stack.state["ports"]["stub"])
    )
    browser = app.start()
    try:
        resp = browser.post(
            "/api/connections/ollama/overlays",
            json={
                "model_id": "llama3-stub",
                "kind": "chat",
                "context_window": 131072,
                "output_limit": 8192,
                "tools": True,
                "residency_scope": "local",
            },
        )
        assert resp.status_code == 200, resp.text

        def offered() -> bool:
            models = browser.get("/conversations/api/models").json()["models"]
            return any(m["id"] == MODEL for m in models)

        wait_for(offered, 60, f"{MODEL} offered in personal mode")
        top = create_project(browser, "Kitchen", instructions=f"KITCHENMARK{stack.run}")
        sub = create_project(browser, "Bread", parent_id=top, instructions=f"BREADMARK{stack.run}")
        found = ok(
            browser.post(
                "/conversations/api/create",
                json={"name": "Sourdough", "model_id": MODEL, "project_id": sub},
            ),
            200,
            201,
        )
        conv = int(found.get("conversation_id") or found.get("id") or found["conversation"]["id"])
        yield Personal(app, browser, top, sub, conv)
    finally:
        app.stop()


def test_personal_projects_have_levels_resources_and_moves_without_sharing(
    stack: Stack, personal: Personal
) -> None:
    """PC15: projects, sub-projects, instructions, resources and moves work in personal
    mode; the members, sharing and transfer routes do not exist there."""
    browser = personal.browser
    note = add_resource(browser, personal.top, "starter.txt", f"STARTER{stack.run}\n".encode())
    first, result = _chat(
        stack, browser, personal.conversation, call64("read_my_file", {"file_id": note})
    )
    system = first["system"]
    assert system.find(f"KITCHENMARK{stack.run}") < system.find(f"BREADMARK{stack.run}"), system[
        -3000:
    ]
    assert system.find(f"KITCHENMARK{stack.run}") >= 0, system[-3000:]
    assert "starter.txt" in system, system[-3000:]
    assert result and f"STARTER{stack.run}" in result, result

    for method, path in (
        ("GET", f"/projects/api/{personal.top}/members"),
        ("POST", f"/projects/api/{personal.top}/members"),
        ("POST", f"/projects/api/{personal.top}/transfer"),
    ):
        resp = browser.request(method, path, json={"user_guid": "x", "role": "viewer"})
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text[:300])
    page = ok(browser.get(f"/projects/api/{personal.top}"))
    assert page["members"] is None, page

    loose = ok(
        browser.post("/conversations/api/create", json={"name": "Loose", "model_id": MODEL}),
        200,
        201,
    )
    loose_id = int(loose.get("conversation_id") or loose.get("id") or loose["conversation"]["id"])
    moved = ok(
        browser.post(
            f"/projects/api/conversations/{loose_id}/move", json={"project_id": personal.top}
        )
    )
    assert moved["to_project"] == personal.top and not moved["widened"], moved
    tree = ok(browser.get("/projects/api"))
    assert tree["shared"] == [], tree
    kitchen = next(p for p in tree["owned"] if p["id"] == personal.top)
    assert [c["name"] for c in kitchen["children"]] == ["Bread"], kitchen
    assert loose_id in [c["id"] for c in kitchen["conversations"]], kitchen


def test_personal_charts_and_the_panel(stack: Stack, personal: Personal) -> None:
    """PC2 to PC5, PC19: a chart made in a project conversation is listed in the panel
    with the attachment and the inherited resources, and renames; nothing is shared."""
    browser = personal.browser
    attached = upload(browser, "rise.txt", b"rise times\n", conversation_id=personal.conversation)
    definition = {
        "type": "bar",
        "data": {"columns": ["Loaf", "Hours"], "rows": [["White", 3], ["Rye", 5]]},
        "encoding": {"x": "Loaf", "y": "Hours"},
    }
    _first, result = _chat(
        stack,
        browser,
        personal.conversation,
        call64("create_chart", {"title": "Rise time by loaf", "definition": definition}),
    )
    assert result, "create_chart was not answered"
    card = preview_card(result)
    view = ok(browser.get(f"/charts/api/{card['file_id']}", params={"theme": "dark"}))
    assert view["type"] == "bar" and view["theme"] == "dark", view
    panel = ok(browser.get(f"/artefacts/api/{personal.conversation}"))
    assert panel["shared"] is False, panel
    assert card["file_id"] in [a["file_id"] for a in panel["generated"]], panel["generated"]
    assert attached in [a["file_id"] for a in panel["provided"]], panel["provided"]
    assert all(a["provided_by"] is None for a in panel["provided"]), panel["provided"]
    # Only the levels holding resources are listed: the top level's starter.txt.
    groups = {g["level"]: [i["name"] for i in g["items"]] for g in panel["project"]}
    assert groups == {1: ["starter.txt"]}, panel["project"]
    renamed = browser.request(
        "PATCH", f"/artefacts/api/{card['file_id']}", json={"title": "Rise by loaf"}
    )
    assert renamed.status_code == 200, renamed.text


def test_personal_mode_offers_the_built_in_skills(stack: Stack, personal: Personal) -> None:
    """PC21: the three built-in skills are listed read-only, offered to the model and load
    with use_skill."""
    browser = personal.browser
    listed = {s["name"]: s for s in ok(browser.get("/skills/api/list"))["skills"]}
    for name in BUILT_IN:
        assert listed.get(name), sorted(listed)
        assert listed[name]["valid"] and listed[name]["enabled"], listed[name]
    since = time.time()
    first, result = _chat(
        stack, browser, personal.conversation, call64("use_skill", {"skill_name": "charts"})
    )
    for name in BUILT_IN:
        assert offers_skill(first["system"], name), first["system"][-4000:]
    assert result and "is loaded" in result, (result or "")[:2000]
    follow = [r for r in chats_since(stack, since) if r["last_role"] == "tool"]
    assert follow and "# Charts" in follow[-1]["system"], "the skill did not reach the prompt"


def test_a_personal_bundle_v2_carries_its_projects_into_a_server_account(
    stack: Stack, admin: Person, configured: None, personal: Personal
) -> None:
    """PC15: the personal user is exported as a version 2 bundle and imported for a
    server user, who owns the projects, their sub-project, instructions, resources and
    conversations, shared with nobody."""
    personal.app.stop()
    target = personal.app.home / "bundle-v2.zip"
    done = export_bundle(personal.app.home, target)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    with zipfile.ZipFile(target) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["format_version"] == 2, manifest
    assert manifest["counts"].get("projects") == 2, manifest["counts"]

    pat = invite_and_accept(stack, admin, f"pat-{stack.run}@example.com", "user")
    try:
        report = admin.browser.post(
            "/admin/bundles",
            files={"bundle": ("bundle-v2.zip", target.read_bytes(), "application/zip")},
            data={"user_id": pat.warden_id},
        )
        assert report.status_code == 200, report.text[:3000]
        tree = ok(pat.browser.get("/projects/api"))
        assert tree["shared"] == [], tree
        kitchen = next(p for p in tree["owned"] if p["name"] == "Kitchen")
        assert kitchen["role"] == "owner", kitchen
        bread = next(c for c in kitchen["children"] if c["name"] == "Bread")
        assert "Sourdough" in [c["name"] for c in bread["conversations"]], bread
        page = ok(pat.browser.get(f"/projects/api/{kitchen['id']}"))
        assert page["project"]["instructions"] == f"KITCHENMARK{stack.run}", page["project"]
        assert page["members"] == [], page["members"]
        resources = page["resources"]
        assert [r["filename"] for r in resources] == ["starter.txt"], resources
        body = pat.browser.get(f"/projects/api/{kitchen['id']}/resources/{resources[0]['file_id']}")
        assert body.status_code == 200 and f"STARTER{stack.run}" in body.text, body.text[:300]
        owners = stack.sql(
            "SELECT DISTINCT user_guid FROM projects WHERE root_id = %s", (kitchen["id"],)
        )
        assert owners == [(pat.guid,)], owners
        assert not stack.sql(
            "SELECT 1 FROM project_members WHERE project_id = %s", (kitchen["id"],)
        )
    finally:
        pat.browser.close()
