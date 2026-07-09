"""Tests for skill discovery, validation, and the resource jail."""

from __future__ import annotations

import pytest

from spark.skills.manager import SkillResourceError, SkillsManager


def make_skill(root, name, description="Does something useful.", body="Do it well.", extra=""):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


class TestDiscovery:
    def test_valid_skill_listed(self, tmp_path) -> None:
        make_skill(tmp_path / "user", "pdf-filler")
        m = SkillsManager(tmp_path / "user")
        skills = m.list_skills()
        assert [s["name"] for s in skills] == ["pdf-filler"]
        assert skills[0]["source"] == "user" and skills[0]["valid"] is True

    def test_bundled_and_user_sources(self, tmp_path) -> None:
        make_skill(tmp_path / "bundled", "skill-creator")
        make_skill(tmp_path / "user", "pdf-filler")
        m = SkillsManager(tmp_path / "user", bundled_dir=tmp_path / "bundled")
        assert {s["name"]: s["source"] for s in m.list_skills()} == {
            "pdf-filler": "user",
            "skill-creator": "bundled",
        }

    def test_user_collision_with_bundled_is_invalid(self, tmp_path) -> None:
        make_skill(tmp_path / "bundled", "skill-creator")
        make_skill(tmp_path / "user", "skill-creator")
        m = SkillsManager(tmp_path / "user", bundled_dir=tmp_path / "bundled")
        assert [s["name"] for s in m.list_skills()] == ["skill-creator"]
        invalid = [s for s in m.list_skills(include_invalid=True) if not s["valid"]]
        assert invalid and "collides" in invalid[0]["error"]

    def test_missing_dir_is_empty_not_error(self, tmp_path) -> None:
        m = SkillsManager(tmp_path / "nowhere" / "skills")
        assert m.list_skills() == []


class TestValidation:
    @pytest.mark.parametrize(
        "name,expected_error",
        [
            ("Bad_Name", "name"),
            ("wrong-folder", "folder"),
            ("no-desc", "description"),
        ],
    )
    def test_invalid_skills_carried_with_reason(self, tmp_path, name, expected_error) -> None:
        if name == "Bad_Name":
            d = tmp_path / "user" / "bad-name"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: x\n---\nbody\n")
        elif name == "wrong-folder":
            d = tmp_path / "user" / "actual-folder"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: wrong-folder\ndescription: x\n---\nbody\n")
        else:
            d = tmp_path / "user" / "no-desc"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: no-desc\n---\nbody\n")
        m = SkillsManager(tmp_path / "user")
        assert m.list_skills() == []
        bad = m.list_skills(include_invalid=True)[0]
        assert bad["valid"] is False and expected_error in bad["error"].lower()

    def test_unknown_frontmatter_keys_ignored(self, tmp_path) -> None:
        make_skill(tmp_path / "user", "tolerant", extra="allowed-tools: Bash\n")
        m = SkillsManager(tmp_path / "user")
        assert m.get_skill("tolerant") is not None


class TestBodyAndResources:
    def test_get_body_strips_frontmatter(self, tmp_path) -> None:
        make_skill(tmp_path / "user", "pdf-filler", body="## Steps\nFill the form.")
        m = SkillsManager(tmp_path / "user")
        body = m.get_body("pdf-filler")
        assert "## Steps" in body and "description:" not in body

    def test_list_and_read_resources(self, tmp_path) -> None:
        d = make_skill(tmp_path / "user", "pdf-filler")
        (d / "references").mkdir()
        (d / "references" / "fields.md").write_text("field notes", encoding="utf-8")
        m = SkillsManager(tmp_path / "user")
        assert m.list_resources("pdf-filler") == ["references/fields.md"]
        assert m.read_resource("pdf-filler", "references/fields.md") == "field notes"

    @pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "references/../../x"])
    def test_jail_violations_refused(self, tmp_path, path) -> None:
        make_skill(tmp_path / "user", "pdf-filler")
        (tmp_path / "outside.md").write_text("secret")
        m = SkillsManager(tmp_path / "user")
        with pytest.raises(SkillResourceError):
            m.read_resource("pdf-filler", path)

    def test_binary_and_oversize_refused(self, tmp_path) -> None:
        d = make_skill(tmp_path / "user", "pdf-filler")
        (d / "blob.bin").write_bytes(b"\x00\x01\x02")
        (d / "big.txt").write_text("x" * (256 * 1024 + 1))
        m = SkillsManager(tmp_path / "user")
        with pytest.raises(SkillResourceError):
            m.read_resource("pdf-filler", "blob.bin")
        with pytest.raises(SkillResourceError):
            m.read_resource("pdf-filler", "big.txt")
