"""End to end: author a skill with create_skill, then trigger and use it."""

from __future__ import annotations

import pytest

from spark.skills.manager import SkillsManager, set_skills_manager
from spark.skills.tools import execute
from spark.skills.trigger import resolve_trigger_for_conversation


@pytest.fixture()
def env(tmp_path):
    m = SkillsManager(tmp_path / "user")
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


def test_author_then_trigger_then_load(env, sync_db) -> None:
    # 1. Author via the tool surface (as the model would after approval)
    text, is_error = execute(
        "create_skill",
        {
            "name": "release-notes",
            "description": "Draft release notes from the changelog.",
            "instructions": "## Steps\nRead CHANGELOG.md and summarise the Unreleased section.",
            "resources": [{"path": "references/style.md", "content": "House style: concise."}],
        },
        {},
    )
    assert not is_error

    # 2. The slash trigger resolves it once enabled (default on)
    rewritten = sync_db.call(
        resolve_trigger_for_conversation, "u1", None, "/release-notes for 0.2.0"
    )
    assert "use_skill('release-notes')" in rewritten

    # 3. use_skill returns the authored instructions and resource listing
    text, is_error = execute("use_skill", {"skill_name": "release-notes"}, {})
    assert not is_error
    assert "summarise the Unreleased section" in text
    assert "references/style.md" in text

    # 4. The bundled resource reads back
    text, is_error = execute(
        "read_skill_resource",
        {"skill_name": "release-notes", "relative_path": "references/style.md"},
        {},
    )
    assert not is_error and "House style" in text
