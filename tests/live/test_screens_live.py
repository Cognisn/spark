"""The projects, chat and skills screens in a real browser (wave 32, kick-off scope 5;
PC2 to PC4, PC16, PC20, PC21).

A headless Chromium, given a signed-in person's session, opens each screen as served
by web in the light and the dark theme at desktop (1400 by 900) and phone (390 by 844)
width: the conversations page with the projects tree (the owner's, and a member's
"Shared with me"), a project page, a project conversation with a chart, its
breadcrumbs and the right-hand panel, and the skills page. Each must start without a
script error, show its parts, and at phone width fit the screen without sideways
scrolling. Screenshots are kept in the sandbox for the session summary.

One case opens the served chat page as it is and asserts it starts without a script
error and draws the conversation's chart card.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.live.conftest import Person, Stack, invite_and_accept
from tests.live.live_helpers import add_resource, create_project, new_conversation, ok, tool_turn

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1500)]

BAR = {
    "type": "bar",
    "data": {"columns": ["Bed", "Plants"], "rows": [["North", 12], ["South", 18], ["East", 7]]},
    "encoding": {"x": "Bed", "y": "Plants"},
    "x_title": "Bed",
    "y_title": "Plants",
}


@dataclass
class Scene:
    owner: Person
    member: Person
    top: int
    sub: int
    conversation: int


@pytest.fixture(scope="module")
def scene(stack: Stack, admin: Person, configured: None, model_id: str) -> Iterator[Scene]:
    owner, member = (
        invite_and_accept(stack, admin, f"{who}-{stack.run}@example.com", "user")
        for who in ("iris", "milo")
    )
    top = create_project(
        owner.browser,
        f"Community garden {stack.run}",
        description="The shared plots at example.org.",
        instructions="## House style\n\n- Metric units\n- Short answers\n",
    )
    sub = create_project(owner.browser, "Seed bank", parent_id=top, instructions="Log each packet.")
    add_resource(owner.browser, top, "plot-map.txt", b"Plots 1 to 12\n")
    ok(
        owner.browser.post(
            f"/projects/api/{top}/members",
            json={"user_id": member.warden_id, "role": "contributor", "scope": "all_conversations"},
        )
    )
    conv = new_conversation(owner, model_id, "Planting plan", project_id=sub)
    tool_turn(
        stack,
        owner,
        conv,
        "Chart the beds.",
        "create_chart",
        {"title": "Plants per bed", "definition": BAR},
    )
    yield Scene(owner, member, top, sub, conv)
    owner.browser.close()
    member.browser.close()


def _shots(stack: Stack) -> Path:
    return stack.sandbox / "browser" / stack.run / "screens"


def test_the_served_chat_page_starts_without_a_script_error(stack: Stack, scene: Scene) -> None:
    """PC20, PC16: the chat page as web serves it starts without an uncaught error and
    draws the conversation's chart card."""
    from tests.live.cdp import DESKTOP, chromium_session, sign_in

    port = int(stack.state["ports"]["web"]) + 61
    with chromium_session(port, _shots(stack) / "downloads") as chrome:
        page = chrome.page()
        page.size(*DESKTOP)
        sign_in(page, scene.owner.browser, "light")
        page.navigate(f"{stack.base_url}/chat/{scene.conversation}")
        page.wait("document.querySelectorAll('.chart-card canvas').length >= 1", 60, "the chart")
        assert not page.errors(), page.errors()


def test_the_screens_in_both_themes_at_desktop_and_phone_width(stack: Stack, scene: Scene) -> None:
    """PC2 to PC4, PC16, PC21: every screen starts cleanly, shows its parts and fits a
    phone's width, in both themes."""
    from tests.live.cdp import DESKTOP, PHONE, chromium_session, sign_in

    port = int(stack.state["ports"]["web"]) + 62
    shots = _shots(stack)
    screens = (
        (
            "conversations",
            scene.owner,
            "/conversations",
            f"document.querySelector('#projects-tree') && "
            f"document.querySelector('#projects-tree').textContent.includes('Community garden')",
        ),
        (
            "shared-with-me",
            scene.member,
            "/conversations",
            "document.querySelector('#projects-tree') && "
            "/shared with me/i.test(document.querySelector('#projects-tree').textContent)",
        ),
        (
            "project-page",
            scene.owner,
            f"/projects/{scene.top}",
            "document.querySelector('#project-page') && "
            "document.querySelector('#project-page').textContent.includes('plot-map.txt') && "
            "document.querySelector('#project-page').textContent.includes('Seed bank')",
        ),
        (
            "chat",
            scene.owner,
            f"/chat/{scene.conversation}",
            "document.querySelectorAll('.chart-card canvas').length >= 1 && "
            "document.querySelector('#project-crumbs-chat') && "
            "document.querySelector('#project-crumbs-chat').textContent.includes('Seed bank')",
        ),
        (
            "skills",
            scene.owner,
            "/skills",
            "document.body.textContent.includes('mermaid-diagrams')",
        ),
    )
    problems: list[str] = []
    for theme in ("light", "dark"):
        for size_name, size in (("desktop", DESKTOP), ("phone", PHONE)):
            for person in (scene.owner, scene.member):
                person_screens = [s for s in screens if s[1] is person]
                if not person_screens:
                    continue
                with chromium_session(port, shots / "downloads") as chrome:
                    page = chrome.page()
                    page.size(*size)
                    sign_in(page, person.browser, theme)
                    for name, _who, path, ready in person_screens:
                        page.navigate(stack.base_url + path)
                        page.wait(
                            f"document.documentElement.getAttribute('data-bs-theme') === '{theme}'",
                            30,
                            f"{name}: the {theme} theme",
                        )
                        page.wait(ready, 90, f"{name} ({theme}, {size_name}) ready")
                        if size_name == "phone":
                            wide = page.evaluate(
                                "document.documentElement.scrollWidth - window.innerWidth"
                            )
                            if wide > 1:
                                problems.append(f"{name} {theme} phone: {wide}px sideways")
                        page.screenshot(shots / f"{name}-{theme}-{size_name}.png")
                    errors = page.errors()
                    if errors:
                        problems.append(f"{theme} {size_name} {person.email}: {errors}")
    assert not problems, problems
