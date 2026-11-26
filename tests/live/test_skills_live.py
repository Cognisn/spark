"""The built-in skills and the administrator's switch on the live stack (wave 32, kick-off
scope 4; PC21).

The three built-in skills (charts, mermaid-diagrams, projects) are offered to every
user's model and load with ``use_skill``. An administrator switches one off for the
instance on web; it is then listed as off and offered to nobody, on either replica,
and ``use_skill`` cannot load it; switched on again it is offered once more. A user
who is not an administrator cannot switch it. What the model was offered is read from
the model stub's record. Users and data are synthetic.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests.live.conftest import Person, Stack, invite_and_accept, wait_for
from tests.live.live_helpers import (
    chats_since,
    instance,
    new_conversation,
    offers_skill,
    ok,
    tool_records,
    turn,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]

BUILT_IN = ("charts", "mermaid-diagrams", "projects")
SETTING = "skills.disabled_builtin"


@pytest.fixture(scope="module")
def user(stack: Stack, admin: Person, configured: None) -> Iterator[Person]:
    person = invite_and_accept(stack, admin, f"una-{stack.run}@example.com", "user")
    yield person
    person.browser.close()


def _listed(person: Person, base_url: str | None = None) -> dict[str, Any]:
    browser = person.browser.on(base_url) if base_url else person.browser
    found: dict[str, Any] = ok(browser.get("/skills/api/list"))
    return found


def _offered(stack: Stack, person: Person, conv: int) -> str:
    """The system prompt the model was sent for one ordinary turn."""
    marker = f"skills-{time.time_ns()}"
    since = time.time()
    turn(person, conv, f"{marker} [[tokens 2]]")
    sent = [r for r in chats_since(stack, since) if marker in r["user"]]
    assert sent, "the turn never reached the model"
    return str(sent[0]["system"])


def test_the_built_in_skills_are_offered_and_load(
    stack: Stack, user: Person, model_id: str
) -> None:
    """PC21: every user sees the three built-in skills, read-only, and their model is
    offered each; use_skill loads the charts skill's instructions."""
    listed = {s["name"]: s for s in _listed(user)["skills"]}
    for name in BUILT_IN:
        skill = listed.get(name)
        assert skill and skill["source"] == "bundled", sorted(listed)
        assert skill["valid"] and skill["enabled"] and skill["instance_enabled"], skill
    assert _listed(user)["can_switch_builtin"] is False
    conv = new_conversation(user, model_id, "Skills")
    system = _offered(stack, user, conv)
    for name in BUILT_IN:
        assert offers_skill(system, name), system[-4000:]
    _e, _first, follow = tool_records(
        stack, user, conv, "Load the chart skill.", "use_skill", {"skill_name": "charts"}
    )
    assert "Skill charts" in follow["last"] and "is loaded" in follow["last"], follow["last"]
    assert "# Charts" in follow["system"], follow["system"][-3000:]


def test_an_administrator_switches_a_built_in_skill_off_and_on_for_everyone(
    stack: Stack, admin: Person, user: Person, model_id: str
) -> None:
    """PC21: switched off by an administrator, a built-in skill is listed as off on both
    replicas, is not offered and cannot be loaded; a user cannot switch it; switched on
    again it is offered."""
    refused = user.browser.post("/settings/api/instance", json={SETTING: ["charts"]})
    assert refused.status_code in (401, 403), refused.text
    conv = new_conversation(user, model_id, "Skills switch")
    instance(admin, {SETTING: ["charts"]})
    try:
        for url in (stack.base_url, stack.web2_url):

            def off(url: str = url) -> bool:
                found = _listed(user, url)
                charts = next(s for s in found["skills"] if s["name"] == "charts")
                return found["switched_off"] == ["charts"] and charts["instance_enabled"] is False

            wait_for(off, 60, f"charts listed as switched off on {url}")
        system = _offered(stack, user, conv)
        assert not offers_skill(system, "charts"), "a switched-off skill was offered"
        assert offers_skill(system, "projects"), system[-4000:]
        assert offers_skill(system, "mermaid-diagrams"), system[-4000:]
        _e, _first, follow = tool_records(
            stack, user, conv, "Load the chart skill.", "use_skill", {"skill_name": "charts"}
        )
        assert "is loaded" not in follow["last"], follow["last"]
        assert "# Charts" not in follow["system"], "use_skill loaded a switched-off skill"
    finally:
        instance(admin, {SETTING: []})

    def on() -> bool:
        return bool(_listed(user)["switched_off"] == [])

    wait_for(on, 60, "charts switched on again")
    assert offers_skill(_offered(stack, user, conv), "charts")
