"""Tests for the skills prompt block and the /skill-name trigger."""

from __future__ import annotations

from spark.skills.prompt import build_skills_block
from spark.skills.trigger import apply_skill_trigger

SKILLS = [
    {"name": "pdf-filler", "description": "Fill PDF forms."},
    {"name": "weekly-report", "description": "Compose the weekly report."},
]


class TestBlock:
    def test_empty_gives_empty_string(self) -> None:
        assert build_skills_block([]) == ""

    def test_block_lists_names_and_instruction(self) -> None:
        block = build_skills_block(SKILLS)
        assert "## Available Skills" in block
        assert "- pdf-filler: Fill PDF forms." in block
        assert "use_skill" in block


class TestTrigger:
    def test_match_prefixes_instruction(self) -> None:
        out = apply_skill_trigger("/pdf-filler fill this form", {"pdf-filler"})
        assert out.startswith("[The user explicitly invoked the skill 'pdf-filler'")
        assert out.endswith("/pdf-filler fill this form")

    def test_no_slash_passthrough(self) -> None:
        assert apply_skill_trigger("hello", {"pdf-filler"}) == "hello"

    def test_unknown_slash_passthrough(self) -> None:
        assert apply_skill_trigger("/unknown thing", {"pdf-filler"}) == "/unknown thing"

    def test_bare_slash_command(self) -> None:
        out = apply_skill_trigger("/pdf-filler", {"pdf-filler"})
        assert "use_skill('pdf-filler')" in out
