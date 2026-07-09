"""Tests for the skills tools and their registry integration."""

from __future__ import annotations

import pytest

from spark.skills.manager import SkillsManager, set_skills_manager
from spark.skills.tools import execute, get_read_tools, get_tools
from tests.test_skills_manager import make_skill


@pytest.fixture()
def manager(tmp_path) -> SkillsManager:
    d = make_skill(tmp_path / "user", "pdf-filler", body="## Steps\nUse the mapping.")
    (d / "references").mkdir()
    (d / "references" / "map.md").write_text("field map", encoding="utf-8")
    m = SkillsManager(tmp_path / "user")
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


class TestDefinitions:
    def test_read_tools_shape(self, manager) -> None:
        names = {t["name"] for t in get_read_tools()}
        assert names == {"use_skill", "read_skill_resource"}
        assert all("inputSchema" in t for t in get_tools())


class TestUseSkill:
    def test_returns_body_path_and_resources(self, manager) -> None:
        text, is_error = execute("use_skill", {"skill_name": "pdf-filler"}, {})
        assert not is_error
        assert "## Steps" in text
        assert "references/map.md" in text
        assert str(manager.skill_path("pdf-filler")) in text

    def test_unknown_skill_lists_available(self, manager) -> None:
        text, is_error = execute("use_skill", {"skill_name": "nope"}, {})
        assert is_error and "pdf-filler" in text


class TestReadResource:
    def test_reads_text(self, manager) -> None:
        text, is_error = execute(
            "read_skill_resource",
            {"skill_name": "pdf-filler", "relative_path": "references/map.md"},
            {},
        )
        assert not is_error and text == "field map"

    def test_jail_violation_is_error(self, manager) -> None:
        text, is_error = execute(
            "read_skill_resource",
            {"skill_name": "pdf-filler", "relative_path": "../secret"},
            {},
        )
        assert is_error and "skill folder" in text


class TestRegistry:
    def test_tools_offered_and_dispatched(self, manager) -> None:
        from spark.tools.registry import execute_builtin_tool, get_builtin_tools

        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert {"use_skill", "read_skill_resource"} <= names
        text, is_error = execute_builtin_tool("use_skill", {"skill_name": "pdf-filler"}, {})
        assert not is_error and "## Steps" in text
