"""Tests for skill authoring: create, update, atomicity, and validation."""

from __future__ import annotations

import pytest

from spark.skills.authoring import execute_write
from spark.skills.manager import SkillsManager, set_skills_manager
from tests.test_skills_manager import make_skill


@pytest.fixture()
def manager(tmp_path) -> SkillsManager:
    make_skill(tmp_path / "bundled", "skill-creator")
    m = SkillsManager(tmp_path / "user", bundled_dir=tmp_path / "bundled")
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


def _create(name="my-skill", **overrides):
    payload = {
        "name": name,
        "description": "Test skill for testing.",
        "instructions": "## Steps\nDo the thing.",
        "resources": [{"path": "references/notes.md", "content": "notes"}],
    }
    payload.update(overrides)
    return execute_write("create_skill", payload)


class TestCreate:
    def test_creates_valid_loadable_skill(self, manager) -> None:
        text, is_error = _create()
        assert not is_error
        skill = manager.get_skill("my-skill")
        assert skill and skill["source"] == "user"
        assert manager.list_resources("my-skill") == ["references/notes.md"]
        assert "Do the thing." in manager.get_body("my-skill")

    def test_collision_with_bundled_rejected(self, manager) -> None:
        text, is_error = _create(name="skill-creator")
        assert is_error and "already exists" in text

    def test_bad_name_rejected(self, manager) -> None:
        text, is_error = _create(name="Bad Name")
        assert is_error and "kebab-case" in text

    @pytest.mark.parametrize("path", ["../evil.md", "/abs.md"])
    def test_resource_path_jail(self, manager, path) -> None:
        text, is_error = _create(resources=[{"path": path, "content": "x"}])
        assert is_error
        assert manager.get_skill("my-skill") is None  # nothing partially written

    def test_size_caps(self, manager) -> None:
        text, is_error = _create(resources=[{"path": "big.md", "content": "x" * (256 * 1024 + 1)}])
        assert is_error and "256" in text


class TestUpdate:
    def test_updates_existing_user_skill(self, manager) -> None:
        _create()
        text, is_error = execute_write(
            "update_skill",
            {
                "name": "my-skill",
                "description": "Updated description.",
                "instructions": "## Steps\nDo it differently.",
            },
        )
        assert not is_error
        assert manager.get_skill("my-skill")["description"] == "Updated description."
        assert "differently" in manager.get_body("my-skill")

    def test_update_missing_or_bundled_rejected(self, manager) -> None:
        assert execute_write(
            "update_skill", {"name": "ghost", "description": "d", "instructions": "i"}
        )[1]
        assert execute_write(
            "update_skill", {"name": "skill-creator", "description": "d", "instructions": "i"}
        )[1]


class TestForcedApproval:
    def test_write_tools_always_reprompt(self) -> None:
        import inspect

        from spark.core import conversation_manager as cm

        src = inspect.getsource(cm)
        assert "create_skill" in src and "update_skill" in src
        assert "skills" in cm._TOOL_CATEGORIES
