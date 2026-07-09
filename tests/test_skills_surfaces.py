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
