"""A turn in a project conversation: the system prompt carries the project block between
the global instructions and the conversation's own (PC7, PC8), a moved conversation takes
the new level's context from its next turn (PC12), and a conversation others see through
its project is a shared one (no personal memories). Personal mode, SQLite."""

from __future__ import annotations

from typing import Any

import pytest

from spark.core.conversation_manager import PRIVATE_TOOLS, ConversationManager
from spark.database import conversations
from spark.database import files as files_db
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.projects.access import ProjectRole, ShareScope
from tests.model_helpers import FakeModels
from tests.test_conversation_manager import StubLLMService

USER = "user-ada-example"
CORA = "user-cora-example"


@pytest.fixture()
def stub_llm() -> StubLLMService:
    return StubLLMService()


@pytest.fixture()
def manager(sync_db: Any, stub_llm: StubLLMService) -> ConversationManager:
    return ConversationManager(
        sync_db, FakeModels(stub_llm), max_tool_iterations=3, global_instructions="GLOBAL RULES"
    )


def project_tree(sync_db: Any) -> tuple[int, int]:
    top = sync_db.call(projects_db.create_project, USER, "Research", instructions="TOP LEVEL RULES")
    low = sync_db.call(
        projects_db.create_project, USER, "Survey", parent_id=top, instructions="LOW LEVEL RULES"
    )
    return top, low


def test_the_project_block_sits_between_global_and_conversation_instructions(
    sync_db: Any, manager: ConversationManager, stub_llm: StubLLMService
) -> None:
    top, low = project_tree(sync_db)
    fid = sync_db.call(
        files_db.record_user_file,
        USER,
        files_db.NewUserFile(
            key="projects/1/brief.txt",
            filename="brief.txt",
            size=12,
            mime_type="text/plain",
            source="project",
        ),
        quota_bytes=None,
    )
    sync_db.call(projects_db.add_resource, USER, top, fid)
    cid = manager.create_conversation("c", "stub-model", USER, instructions="CONVERSATION RULES")
    sync_db.call(projects_db.place_conversation, USER, cid, low)
    sync_db.call(conversations.update_conversation, cid, USER, memory_enabled=0, rag_enabled=0)

    manager.send_message(cid, "hello", USER, tools=[])
    system = str(stub_llm.last_system)
    order = [
        system.index("GLOBAL RULES"),
        system.index("## Project instructions"),
        system.index("TOP LEVEL RULES"),
        system.index("LOW LEVEL RULES"),
        system.index(f"file {fid}"),
        system.index("CONVERSATION RULES"),
    ]
    assert order == sorted(order)


def test_a_moved_conversation_takes_the_new_level_next_turn(
    sync_db: Any, manager: ConversationManager, stub_llm: StubLLMService
) -> None:
    top, low = project_tree(sync_db)
    cid = manager.create_conversation("c", "stub-model", USER)
    sync_db.call(conversations.update_conversation, cid, USER, memory_enabled=0, rag_enabled=0)
    sync_db.call(projects_db.place_conversation, USER, cid, low)
    manager.send_message(cid, "one", USER, tools=[])
    assert "LOW LEVEL RULES" in str(stub_llm.last_system)
    sync_db.call(projects_db.move_conversation, USER, cid, top, confirm=True)
    manager.send_message(cid, "two", USER, tools=[])
    assert "LOW LEVEL RULES" not in str(stub_llm.last_system)
    assert "TOP LEVEL RULES" in str(stub_llm.last_system)
    sync_db.call(projects_db.move_conversation, USER, cid, None, confirm=True)
    manager.send_message(cid, "three", USER, tools=[])
    assert "## Project instructions" not in str(stub_llm.last_system)


def test_no_block_out_of_a_project(
    manager: ConversationManager, stub_llm: StubLLMService, sync_db: Any
) -> None:
    cid = manager.create_conversation("c", "stub-model", USER)
    sync_db.call(conversations.update_conversation, cid, USER, memory_enabled=0, rag_enabled=0)
    manager.send_message(cid, "hello", USER, tools=[])
    assert "## Project instructions" not in str(stub_llm.last_system)


def test_a_conversation_seen_through_its_project_is_shared(
    sync_db: Any, manager: ConversationManager, stub_llm: StubLLMService
) -> None:
    top, _ = project_tree(sync_db)
    sync_db.call(
        members_db.share, USER, top, CORA, ProjectRole.VIEWER, ShareScope.ALL_CONVERSATIONS
    )
    cid = manager.create_conversation("c", "stub-model", USER)
    sync_db.call(conversations.update_conversation, cid, USER, memory_enabled=0, rag_enabled=0)
    sync_db.call(projects_db.place_conversation, USER, cid, top)
    tools = [{"name": name, "description": "", "inputSchema": {}} for name in PRIVATE_TOOLS]
    tools.append({"name": "kept_tool", "description": "", "inputSchema": {}})
    manager.send_message(cid, "hello", USER, tools=tools)
    system = str(stub_llm.last_system)
    assert "**Shared conversation:**" in system
