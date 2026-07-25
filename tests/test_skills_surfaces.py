"""Skills must be advertised on every LLM surface."""

from __future__ import annotations

import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.skills.manager import SkillsManager, set_skills_manager
from tests.test_skills_manager import make_skill


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


@pytest.fixture()
def skills_env(tmp_path):
    make_skill(tmp_path / "user", "pdf-filler", description="Fill PDF forms.")
    m = SkillsManager(tmp_path / "user")
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


class TestChatSurface:
    def test_system_instructions_contain_block(self, skills_env, db) -> None:
        from spark.core.conversation_manager import ConversationManager
        from spark.llm.context_limits import ContextLimitResolver
        from spark.llm.manager import LLMManager
        from tests.test_conversation_manager import StubLLMService

        stub = StubLLMService()
        llm = LLMManager()
        llm.register_provider(stub)  # type: ignore[arg-type]
        mgr = ConversationManager(db, llm, ContextLimitResolver(), max_tool_iterations=5)
        cid = mgr.create_conversation("t", "stub-model", "u1")
        mgr.send_message(cid, "hello", "u1")
        assert "## Available Skills" in stub.last_system
        assert "pdf-filler: Fill PDF forms." in stub.last_system


class TestAgentSurface:
    def test_agent_system_contains_block(self, skills_env, db) -> None:
        from unittest.mock import MagicMock

        from spark.core.agent_executor import AgentExecutor

        ex = AgentExecutor(MagicMock(), db, {"embedded_tools": {}}, user_guid="u1")
        system = ex._build_system("worker", "do a task", "orchestrator")
        assert "## Available Skills" in system

    def test_agent_block_absent_without_skills(self, db, tmp_path) -> None:
        from unittest.mock import MagicMock

        from spark.core.agent_executor import AgentExecutor
        from spark.skills.manager import SkillsManager, set_skills_manager

        set_skills_manager(SkillsManager(tmp_path / "empty-skills"))
        try:
            ex = AgentExecutor(MagicMock(), db, {"embedded_tools": {}}, user_guid="u1")
            assert "## Available Skills" not in ex._build_system("w", "t", "orchestrator")
        finally:
            set_skills_manager(None)


class TestActionSurface:
    def test_action_system_contains_block(self, skills_env, db) -> None:
        from unittest.mock import MagicMock

        from spark.scheduler.executor import ActionExecutor

        ctx = MagicMock()
        ctx.settings.get.side_effect = lambda key, default=None, **kw: default
        ex = ActionExecutor(ctx, "daemon-1")
        ex._user_guid = "u1"
        action = {
            "name": "A",
            "description": "D",
            "action_prompt": "p",
            "model_id": "m",
            "max_tokens": 4096,
        }
        system = ex._build_action_system(db, action, [], 4096, "fresh")
        assert "## Available Skills" in system
        assert "You are Spark executing an autonomous action." in system


class TestDebateSurface:
    def test_prompts_accept_block(self) -> None:
        from spark.core.debate import prompts

        d = prompts.debater_system("pro", "T", None, skills_block="## Available Skills\n- x: y")
        j = prompts.judge_system(
            "T",
            None,
            rounds_mode="fixed",
            max_rounds=1,
            skills_block="## Available Skills\n- x: y",
        )
        assert "## Available Skills" in d and "## Available Skills" in j

    def test_judge_gets_read_tools_never_run_command(self, skills_env, db) -> None:
        from spark.core.debate.orchestrator import DebateOrchestrator
        from spark.database import debates
        from tests.test_debate_orchestrator import (
            AGENTS,
            ScriptedService,
            text_response,
            tool_response,
        )

        captured: list = []

        class RecordingService(ScriptedService):
            def invoke_model(self, messages, **kwargs):
                captured.append(kwargs)
                return super().invoke_model(messages, **kwargs)

        ph = db.placeholder
        cur = db.execute(
            f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
            f"VALUES ({ph}, {ph}, {ph}, 'debate')",
            ("D", "model-judge", "u1"),
        )
        db.commit()
        cid = cur.lastrowid
        debates.create_debate(db, cid, "Topic T", "fixed", 1, "u1", AGENTS)

        services = {
            "model-judge": RecordingService(
                [
                    tool_response("set_speaking_order", {"first_speaker": "pro"}, "Open."),
                    text_response("Ruling."),
                ]
            ),
            "model-pro": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "P."})]
            ),
            "model-con": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "C."})]
            ),
        }
        orch = DebateOrchestrator(db, lambda m: services[m], {}, status_callback=lambda t, d: None)
        orch.run(cid, "u1")

        judge_tool_names = {
            t["name"] for call in captured if call.get("tools") for t in call["tools"]
        }
        assert {"use_skill", "read_skill_resource"} <= judge_tool_names
        assert "run_command" not in judge_tool_names
        assert any("## Available Skills" in (c.get("system") or "") for c in captured)
