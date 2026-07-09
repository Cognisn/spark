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
        mgr = ConversationManager(
            db, llm, ContextLimitResolver(), max_tool_iterations=5
        )
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
            assert "## Available Skills" not in ex._build_system(
                "w", "t", "orchestrator"
            )
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
