"""Compaction on conduit's compaction layer, live settings, and the restore path's scoping.

* conduit's compactor decides and cuts; SPARK writes the summary and keeps its record
  (rolled-up messages behind a ``[COMPACTED CONTEXT ...]`` message, read first);
* past the emergency threshold with no summary to be had, conduit's ``KeepLastTurns``
  leaves SPARK's truncation notice;
* the thresholds and prompt caching follow a server's instance settings at each turn;
* the stored conversation a turn restores its skills from is the user's own, on both
  engines.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.connections.transcript_store import skill_transcript
from spark.core.context_compaction import (
    CompactionSettings,
    ContextCompactor,
    ordered_for_model,
)
from spark.core.conversation_manager import ConversationManager
from spark.database import conversations, messages
from spark.database import skills as skills_db
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.model_helpers import FakeModels, FixedLimits

ADA = "user-ada-example"
GRACE = "user-grace-example"


class Summariser:
    """A model service whose every answer is ``text``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def invoke_model(self, history: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"content": self.text, "stop_reason": "end_turn", "usage": {}}


SUMMARY = "Summary of the earlier conversation: " + "detail " * 60


def _conversation(sync_db: Any, turns: int, user: str = ADA) -> int:
    cid: int = sync_db.call(conversations.create_conversation, "Long", "stub-model", user)
    for i in range(turns):
        sync_db.call(messages.add_message, cid, "user", f"question {i}", 50, user)
        sync_db.call(messages.add_message, cid, "assistant", f"answer {i}", 50, user)
    return cid


def _compactor(sync_db: Any, service: Any, **settings: float) -> ContextCompactor:
    return ContextCompactor(service, sync_db, FixedLimits(), **settings)


class TestConduitCompaction:
    def test_summarises_the_older_turns_and_keeps_the_last(self, sync_db: Any) -> None:
        cid = _conversation(sync_db, 3)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=100_000)
        service = Summariser(SUMMARY)
        assert _compactor(sync_db, service, threshold=0.01).check_and_compact(cid, "m", ADA)
        active = ordered_for_model(sync_db.call(messages.get_messages, ADA, cid))
        assert active[0]["content"].startswith("[COMPACTED CONTEXT")
        assert SUMMARY.strip() in active[0]["content"]
        assert [r["content"] for r in active[1:]] == ["answer 2"]
        assert service.calls == 1

    def test_a_conversation_that_fits_is_left_unless_it_gets_under_the_threshold(
        self, sync_db: Any
    ) -> None:
        # Fits the window (8192 with a 4096 output budget) at 37% of it, over a 0.2
        # threshold; but the summary's allowance (2000 tokens) alone is 24% of the
        # window, so no summary could bring it under the threshold.
        cid = _conversation(sync_db, 3)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=3_000)
        service = Summariser(SUMMARY)
        compactor = _compactor(sync_db, service, threshold=0.2, emergency_threshold=0.99)
        assert compactor.check_and_compact(cid, "m", ADA) is False
        assert service.calls == 0  # no summary paid for that could not help

    def test_past_the_emergency_threshold_without_a_summary_it_truncates(
        self, sync_db: Any
    ) -> None:
        cid = _conversation(sync_db, 3)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=100_000)
        service = Summariser("too short")
        assert _compactor(sync_db, service, threshold=0.01).check_and_compact(cid, "m", ADA)
        active = ordered_for_model(sync_db.call(messages.get_messages, ADA, cid))
        assert active[0]["content"].startswith("[EMERGENCY TRUNCATION")
        assert [r["content"] for r in active[1:]] == ["answer 2"]

    def test_below_the_emergency_threshold_a_failed_summary_changes_nothing(
        self, sync_db: Any
    ) -> None:
        cid = _conversation(sync_db, 3)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=7_000)
        compactor = _compactor(
            sync_db, Summariser("too short"), threshold=0.5, emergency_threshold=0.99
        )
        assert compactor.check_and_compact(cid, "m", ADA) is False
        assert len(sync_db.call(messages.get_messages, ADA, cid)) == 6

    def test_another_users_conversation_is_never_compacted(self, sync_db: Any) -> None:
        cid = _conversation(sync_db, 3)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=100_000)
        service = Summariser(SUMMARY)
        assert (
            _compactor(sync_db, service, threshold=0.01).check_and_compact(cid, "m", GRACE) is False
        )
        assert service.calls == 0


# -- Live settings ---------------------------------------------------------------------


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str) -> Any:
        return self.values.get(key)


@pytest.fixture()
def server_settings() -> Iterator[_Settings]:
    from spark.instance_settings import accessor

    source = _Settings(
        {
            "conversation.rollup_threshold": 0.4,
            "conversation.rollup_summary_ratio": 0.2,
            "conversation.emergency_rollup_threshold": 0.9,
            "conversation.prompt_caching": True,
        }
    )
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y"))
    )
    accessor.configure(source)
    yield source
    accessor.reset()
    mode.reset()


def _manager(sync_db: Any, **kwargs: Any) -> ConversationManager:
    return ConversationManager(sync_db, FakeModels(Summariser(SUMMARY)), **kwargs)


class TestLiveSettings:
    def test_a_server_reads_its_thresholds_at_each_check(
        self, sync_db: Any, server_settings: _Settings
    ) -> None:
        mgr = _manager(sync_db, rollup_threshold=0.7)
        assert mgr._live_compaction_settings() == CompactionSettings(0.4, 0.9, 0.2)
        server_settings.values["conversation.rollup_threshold"] = 0.6
        assert mgr._live_compaction_settings().threshold == 0.6
        server_settings.values["conversation.rollup_threshold"] = "not a number"
        assert mgr._live_compaction_settings().threshold == 0.7

    def test_the_check_uses_the_settings_in_force(
        self, sync_db: Any, server_settings: _Settings
    ) -> None:
        mgr = _manager(sync_db)
        server_settings.values["conversation.rollup_threshold"] = 0.25
        mgr._check_compaction(_conversation(sync_db, 1), "stub-model", ADA)
        assert mgr._compactor is not None
        assert mgr._compactor.settings.threshold == 0.25

    def test_prompt_caching_follows_the_setting_unless_the_conversation_chose(
        self, sync_db: Any, server_settings: _Settings
    ) -> None:
        mgr = _manager(sync_db, prompt_caching=True)
        assert mgr._live_prompt_caching({"prompt_caching": None}) is True
        server_settings.values["conversation.prompt_caching"] = False
        assert mgr._live_prompt_caching({"prompt_caching": None}) is False
        assert mgr._live_prompt_caching({"prompt_caching": 1}) is True

    def test_personal_mode_uses_the_managers_own_values(self, sync_db: Any) -> None:
        mode.reset()
        mgr = _manager(sync_db, rollup_threshold=0.7, prompt_caching=True)
        mgr.update_compaction_settings(rollup_threshold=0.5)
        mgr.update_prompt_caching(False)
        assert mgr._live_compaction_settings().threshold == 0.5
        assert mgr._live_prompt_caching(None) is False
        mgr.update_compaction_settings(rollup_threshold=7.0)  # out of range: the default
        assert mgr._live_compaction_settings().threshold == 0.7


# -- The restore path's data on both engines ---------------------------------------------


def _use(tool_id: str, skill: str) -> str:
    block = {"type": "tool_use", "id": tool_id, "name": "use_skill", "input": {"skill_name": skill}}
    return json.dumps([block])


def _ok(tool_id: str) -> str:
    return "[TOOL_RESULTS]" + json.dumps(
        [{"type": "tool_result", "tool_use_id": tool_id, "content": "loaded"}]
    )


class _Manager:
    def list_skills(self, include_invalid: bool = False) -> list[dict[str, Any]]:
        return [{"name": n, "description": n, "valid": True} for n in ("pdf-filler", "notes")]


class TestRestoreScoping:
    async def test_loaded_skills_come_from_the_users_own_conversation(self, spark_db: Any) -> None:
        cid = await conversations.create_conversation(spark_db, "c", "m", ADA)
        await messages.add_message(spark_db, cid, "user", "fill it", 1, ADA)
        first = await messages.add_message(
            spark_db, cid, "assistant", _use("t1", "pdf-filler"), 1, ADA
        )
        await messages.add_message(spark_db, cid, "user", _ok("t1"), 1, ADA)
        # A compaction rolls those up; the skill stays loaded
        await messages.apply_compaction(
            spark_db,
            cid,
            "[COMPACTED CONTEXT x]\n\nsummary",
            1,
            [first],
            ADA,
            summary="summary",
            original_tokens=1,
        )
        own = await messages.get_messages(spark_db, ADA, cid, include_rolled_up=True)
        assert skill_transcript(str(cid), own).loaded_skills() == {"pdf-filler"}
        theirs = await messages.get_messages(spark_db, GRACE, cid, include_rolled_up=True)
        assert skill_transcript(str(cid), theirs).loaded_skills() == frozenset()

    async def test_enabled_skills_are_each_users_own(self, spark_db: Any) -> None:
        await skills_db.set_skill_enabled(spark_db, "pdf-filler", False, ADA)
        ada = await skills_db.resolve_enabled(spark_db, _Manager(), ADA)
        grace = await skills_db.resolve_enabled(spark_db, _Manager(), GRACE)
        assert [s["name"] for s in ada] == ["notes"]
        assert [s["name"] for s in grace] == ["pdf-filler", "notes"]
