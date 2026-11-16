"""SPARK's skills on conduit: the skill source, progressive disclosure and the cached prefix.

The source lists and loads SPARK's bundled and user skills through conduit's
filesystem source (conduit's SKILL.md validation and confined reads), keeps SPARK's
uniqueness rule (a bundled skill wins its name) and serves one user's enabled skills
only. In a chat turn ``use_skill`` loads a skill into the system prompt (before the
prompt's cache breakpoint), never into a tool result, and later turns restore it in
order from the stored conversation, so the prefix stays byte-identical.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conduit.core.backends import Skill, SkillSource
from conduit.skills import SkillNotFound
from conduit.testing.conformance import SkillSourceSuite

from spark.connections.transcript_store import skill_transcript, skill_uses
from spark.core.context_compaction import CompactionSettings
from spark.core.conversation_manager import ConversationManager
from spark.database import conversations, messages
from spark.database import skills as skills_db
from spark.skills.manager import SkillsManager, set_skills_manager
from spark.skills.session import TurnSkills
from spark.skills.source import SparkSkillSource
from spark.skills.tools import SESSION_KEY, execute
from tests.model_helpers import FakeModels
from tests.test_skills_manager import make_skill

ADA = "user-ada-example"
GRACE = "user-grace-example"


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    bundled, user = tmp_path / "bundled", tmp_path / "user"
    make_skill(bundled, "skill-creator", description="Build a skill.", body="Bundled body.")
    make_skill(user, "skill-creator", description="An impostor.", body="User body.")
    d = make_skill(user, "pdf-filler", description="Fill PDF forms.", body="## Steps\nFill it.")
    (d / "references").mkdir()
    (d / "references" / "map.md").write_text("field map", encoding="utf-8")
    make_skill(user, "release-notes", description="Draft release notes.", body="Read the log.")
    return bundled, user


@pytest.fixture()
def skills_manager(roots: tuple[Path, Path]) -> Any:
    bundled, user = roots
    m = SkillsManager(user, bundled_dir=bundled)
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


# -- The source --------------------------------------------------------------------


class TestSparkSkillSource:
    def test_lists_both_folders_and_the_bundled_skill_wins_its_name(self, roots) -> None:
        bundled, user = roots
        source = SparkSkillSource(user, bundled)
        listed = _run(source.list_skills())
        assert [s.name for s in listed] == ["pdf-filler", "release-notes", "skill-creator"]
        loaded = _run(source.load_skill("skill-creator"))
        assert loaded.body.strip() == "Bundled body."
        assert _run(source.origin("skill-creator")) == "bundled"
        assert _run(source.origin("pdf-filler")) == "user"

    def test_serves_only_the_users_enabled_skills(self, roots) -> None:
        bundled, user = roots
        source = SparkSkillSource(user, bundled, enabled={"pdf-filler"})
        assert [s.name for s in _run(source.list_skills())] == ["pdf-filler"]
        with pytest.raises(SkillNotFound):
            _run(source.load_skill("release-notes"))
        assert _run(source.origin("release-notes")) is None

    def test_an_invalid_skill_is_left_out(self, roots) -> None:
        bundled, user = roots
        make_skill(user, "strict", extra="author: someone\n")
        source = SparkSkillSource(user, bundled)
        assert "strict" not in [s.name for s in _run(source.list_skills())]

    def test_missing_folders_are_empty(self, tmp_path: Path) -> None:
        source = SparkSkillSource(tmp_path / "none", tmp_path / "also-none")
        assert _run(source.list_skills()) == []


def _write_skill(root: Path, skill: Skill) -> None:
    folder = root / skill.summary.name
    folder.mkdir(parents=True)
    description = skill.summary.description.replace('"', '\\"')
    version = f"version: {skill.summary.version}\n" if skill.summary.version else ""
    (folder / "SKILL.md").write_text(
        f'---\nname: {skill.summary.name}\ndescription: "{description}"\n{version}---\n'
        f"{skill.body}",
        encoding="utf-8",
        newline="\n",
    )
    for path, text in skill.resources.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


class TestSparkSkillSourceConformance(SkillSourceSuite):
    """conduit's skill source suite, with the skills split across SPARK's two folders."""

    async def make_source(self, skills: Sequence[Skill]) -> SkillSource:
        root = Path(tempfile.mkdtemp(prefix="spark-skills-"))
        for index, skill in enumerate(skills):
            _write_skill(root / ("bundled" if index % 2 else "user"), skill)
        (root / "bundled").mkdir(exist_ok=True)
        (root / "user").mkdir(exist_ok=True)
        source = SparkSkillSource(root / "user", root / "bundled")
        self._roots = [*getattr(self, "_roots", []), root]
        return source

    async def close_source(self, source: SkillSource) -> None:
        for root in getattr(self, "_roots", []):
            shutil.rmtree(root, ignore_errors=True)
        self._roots = []


# -- The turn's session ---------------------------------------------------------------


def _turn(m: SkillsManager, enabled: set[str] | None = None) -> TurnSkills:
    return TurnSkills(m.source(enabled=enabled), folders=m.folders())


class TestTurnSkills:
    def test_lists_then_loads_into_the_system_prompt(self, skills_manager) -> None:
        skills = _turn(skills_manager)
        try:
            before = skills.system("You are Spark.")
            assert before.startswith("You are Spark.\n\n## Available Skills")
            assert "- pdf-filler: Fill PDF forms." in before
            assert "Fill it." not in before

            text, is_error = skills.use("pdf-filler")
            assert not is_error
            assert "Fill it." not in text  # the body goes to the prompt, not the result
            assert "references/map.md" in text

            after = skills.system("You are Spark.")
            assert after.startswith(before)
            assert '<skill name="pdf-filler">' in after and "Fill it." in after
            assert str(skills_manager.skill_path("pdf-filler")) in after
        finally:
            skills.close()

    def test_resources_are_read_once_the_skill_is_loaded(self, skills_manager) -> None:
        skills = _turn(skills_manager)
        try:
            text, is_error = skills.use("pdf-filler", "references/map.md")
            assert is_error and "use_skill first" in text
            skills.use("pdf-filler")
            assert skills.use("pdf-filler", "references/map.md") == ("field map", False)
            text, is_error = skills.use("pdf-filler", "../secret")
            assert is_error and "secret" not in text
        finally:
            skills.close()

    def test_a_skill_the_user_has_not_enabled_cannot_be_loaded(self, skills_manager) -> None:
        skills = _turn(skills_manager, enabled={"release-notes"})
        try:
            text, is_error = skills.use("pdf-filler")
            assert is_error and "release-notes" in text and "pdf-filler" not in text
            assert skills.loaded == ()
            assert "pdf-filler" not in skills.system("base")
        finally:
            skills.close()

    def test_restored_skills_give_the_same_prefix(self, skills_manager) -> None:
        first = _turn(skills_manager)
        try:
            first.use("release-notes")
            first.use("pdf-filler")
            prefix = first.system("base")
        finally:
            first.close()
        rows = [
            _tool_call("t1", "release-notes"),
            _tool_result("t1"),
            _tool_call("t2", "pdf-filler"),
            _tool_result("t2"),
        ]
        again = _turn(skills_manager)
        try:
            again.restore(skill_transcript("1", rows))
            assert again.loaded == ("release-notes", "pdf-filler")
            assert again.system("base") == prefix
        finally:
            again.close()

    def test_the_tools_go_to_the_session_when_a_turn_gives_one(self, skills_manager) -> None:
        skills = _turn(skills_manager)
        try:
            text, is_error = execute(
                "use_skill", {"skill_name": "pdf-filler"}, {SESSION_KEY: skills}
            )
            assert not is_error and "Fill it." not in text
            assert skills.loaded == ("pdf-filler",)
            assert execute(
                "read_skill_resource",
                {"skill_name": "pdf-filler", "relative_path": "references/map.md"},
                {SESSION_KEY: skills},
            ) == ("field map", False)
        finally:
            skills.close()
        # Without a session (debates, panels, sub-agents) the body is the result, as before
        text, is_error = execute("use_skill", {"skill_name": "pdf-filler"}, {})
        assert not is_error and "Fill it." in text


# -- Markers from the stored conversation --------------------------------------------


def _tool_call(tool_id: str, skill: str, **extra: Any) -> dict[str, Any]:
    block = {"type": "tool_use", "id": tool_id, "name": "use_skill", "input": {"skill_name": skill}}
    block["input"].update(extra)
    return {"role": "assistant", "content": json.dumps([block])}


def _tool_result(tool_id: str, *, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
    if error:
        result["is_error"] = True
    return {"role": "user", "content": "[TOOL_RESULTS]" + json.dumps([result])}


class TestSkillUses:
    def test_successful_loads_in_order(self) -> None:
        rows = [
            {"role": "user", "content": "hello"},
            _tool_call("a", "release-notes"),
            _tool_result("a"),
            _tool_call("b", "pdf-filler"),
            _tool_result("b", error=True),  # refused: not loaded
            _tool_call("c", "pdf-filler", resource="references/map.md"),  # a read, not a load
            _tool_result("c"),
            _tool_call("d", "Not A Name"),
            _tool_result("d"),
        ]
        assert [m.skill for m in skill_uses(rows)] == ["release-notes"]
        transcript = skill_transcript("7", rows)
        assert transcript.loaded_skills() == frozenset({"release-notes"})
        assert [e.seq for e in transcript.entries] == [1]


# -- Authoring stays SPARK's, checked by conduit ------------------------------------------


class TestAuthoringValidation:
    def test_what_conduit_would_refuse_is_never_written(self, skills_manager) -> None:
        from spark.skills.authoring import execute_write

        text, is_error = execute_write(
            "create_skill",
            {"name": "tabbed", "description": "One\tline", "instructions": "Do it."},
        )
        assert is_error and "(description_invalid)" in text
        assert not (skills_manager.user_dir / "tabbed").exists()

    def test_resource_paths_follow_conduits_rule(self, skills_manager) -> None:
        from spark.skills.authoring import execute_write

        text, is_error = execute_write(
            "create_skill",
            {
                "name": "spaced",
                "description": "Spaced resources.",
                "instructions": "Do it.",
                "resources": [{"path": "refs/my notes.md", "content": "x"}],
            },
        )
        assert is_error and "relative path" in text
        assert not (skills_manager.user_dir / "spaced").exists()

    def test_a_description_with_quotes_and_colons_round_trips(self, skills_manager) -> None:
        from spark.skills.authoring import execute_write

        description = 'Use when asked: "summarise #42" or C:\\path'
        text, is_error = execute_write(
            "create_skill",
            {"name": "quoted", "description": description, "instructions": "Do it."},
        )
        assert not is_error, text
        assert skills_manager.get_skill("quoted")["description"] == description
        listed = _run(skills_manager.source().list_skills())
        assert {s.name: s.description for s in listed}["quoted"] == description


# -- The chat turn --------------------------------------------------------------------


class RecordingService:
    """A model service that plays SPARK-format responses and records each call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def invoke_model(self, history: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": history, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return _text("Done.")


def _text(text: str) -> dict[str, Any]:
    return {
        "content": text,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_use": None,
        "content_blocks": [{"type": "text", "text": text}],
    }


def _use(tool_id: str, skill: str) -> dict[str, Any]:
    call = {"type": "tool_use", "id": tool_id, "name": "use_skill", "input": {"skill_name": skill}}
    return {
        "content": "",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_use": [call],
        "content_blocks": [call],
    }


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The system prompt carries the time; hold it still so prompts compare byte for byte."""
    import datetime as real

    from spark.core import conversation_manager as cm

    class Frozen(real.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
            return cls(2026, 11, 16, 9, 0, 0, tzinfo=tz)

    monkeypatch.setattr(cm, "datetime", Frozen)


def _chat(sync_db: Any, service: RecordingService, user: str = ADA) -> tuple[Any, int]:
    mgr = ConversationManager(sync_db, FakeModels(service), max_tool_iterations=5)
    cid = mgr.create_conversation("Skills", "stub-model", user)
    sync_db.call(conversations.update_conversation, cid, user, memory_enabled=0, rag_enabled=0)
    return mgr, cid


def _skill_tools() -> list[dict[str, Any]]:
    from spark.skills.tools import get_read_tools

    return get_read_tools()


class TestChatTurn:
    def test_use_skill_loads_into_the_next_calls_system_prompt(
        self, sync_db: Any, skills_manager: Any, frozen_clock: None
    ) -> None:
        service = RecordingService([_use("t1", "pdf-filler"), _text("Filled.")])
        mgr, cid = _chat(sync_db, service)
        result = mgr.send_message(cid, "Fill this form", ADA, tools=_skill_tools())
        assert result["content"] == "Filled."
        first, second = (call["system"] for call in service.calls)
        assert "- pdf-filler: Fill PDF forms." in first and "Fill it." not in first
        assert second.startswith(first) and "Fill it." in second
        # the tool result told the model the skill is loaded; the body is not in the messages
        results = json.dumps(service.calls[1]["messages"])
        assert "Fill it." not in results and "pdf-filler" in results

    def test_the_next_turn_restores_the_loaded_skill_byte_for_byte(
        self, sync_db: Any, skills_manager: Any, frozen_clock: None
    ) -> None:
        service = RecordingService([_use("t1", "pdf-filler"), _text("Filled.")])
        mgr, cid = _chat(sync_db, service)
        mgr.send_message(cid, "Fill this form", ADA, tools=_skill_tools())
        mgr.send_message(cid, "And another", ADA, tools=_skill_tools())
        loaded_system = service.calls[1]["system"]
        assert service.calls[2]["system"] == loaded_system

    def test_another_users_choices_never_reach_a_turn(
        self, sync_db: Any, skills_manager: Any, frozen_clock: None
    ) -> None:
        sync_db.call(skills_db.set_skill_enabled, "pdf-filler", False, ADA)
        service = RecordingService([_use("t1", "pdf-filler"), _text("Cannot.")])
        mgr, cid = _chat(sync_db, service, ADA)
        mgr.send_message(cid, "Fill this form", ADA, tools=_skill_tools())
        first, second = (call["system"] for call in service.calls)
        assert "pdf-filler" not in first and "Fill it." not in second
        refused = json.dumps(service.calls[1]["messages"])
        assert "No skill of that name is available" in refused

        # Grace has not turned it off: her turn lists and loads it
        grace = RecordingService([_use("t2", "pdf-filler"), _text("Filled.")])
        mgr_g, cid_g = _chat(sync_db, grace, GRACE)
        mgr_g.send_message(cid_g, "Fill this form", GRACE, tools=_skill_tools())
        assert "Fill it." in grace.calls[1]["system"]

    def test_a_conversations_own_override_is_honoured(
        self, sync_db: Any, skills_manager: Any, frozen_clock: None
    ) -> None:
        service = RecordingService([_text("Hello.")])
        mgr, cid = _chat(sync_db, service)
        sync_db.call(skills_db.set_conversation_skill_enabled, ADA, cid, "release-notes", False)
        mgr.send_message(cid, "Hi", ADA, tools=_skill_tools())
        system = service.calls[0]["system"]
        assert "pdf-filler" in system and "release-notes" not in system


# -- The cached prefix across a compaction ----------------------------------------------


class TestPrefixAcrossCompaction:
    def test_compaction_leaves_the_system_prompt_and_loaded_skills_unchanged(
        self, sync_db: Any, skills_manager: Any, frozen_clock: None
    ) -> None:
        summary = "Summary of the earlier conversation: " + "detail " * 60
        service = RecordingService(
            [_use("t1", "pdf-filler"), _text("Filled."), _text(summary), _text("Next.")]
        )
        mgr, cid = _chat(sync_db, service)
        mgr.send_message(cid, "Fill this form", ADA, tools=_skill_tools())
        before = service.calls[1]["system"]

        # Force a compaction: a conversation far past its threshold
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=100_000)
        assert mgr._compactor is not None
        mgr._compactor.update(CompactionSettings(threshold=0.01))
        assert mgr._compactor.check_and_compact(cid, "stub-model", ADA, service=service)
        stored = sync_db.call(messages.get_messages, ADA, cid)
        assert stored[-1]["content"].startswith("[COMPACTED CONTEXT")

        mgr.send_message(cid, "And another", ADA, tools=_skill_tools())
        after = service.calls[-1]
        assert after["system"] == before  # the cached prefix is byte-identical
        history = after["messages"]
        assert history[0]["content"].startswith("[COMPACTED CONTEXT")  # summary first
        assert history[1]["role"] == "assistant"
