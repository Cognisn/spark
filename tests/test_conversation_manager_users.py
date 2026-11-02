"""Two users' turns on one shared ConversationManager each act for their own user.

In server mode one ConversationManager serves every signed-in user. A turn's
memory tools, memory retrieval and sub-agents must use that turn's user guid,
passed down the call, never instance state another turn may overwrite and never
the process's own guid.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from spark.core.conversation_manager import ConversationManager
from spark.llm.context_limits import ContextLimitResolver
from spark.llm.manager import LLMManager
from tests.test_conversation_manager import StubLLMService

ADA = "user-ada-example"
GRACE = "user-grace-example"
PROCESS = "process-guid"

AGENTS_ON = {"embedded_tools": {"agents": {"enabled": True, "default_mode": "orchestrator"}}}


class RecordingMemoryIndex:
    """Stands in for MemoryIndex: records whose memories each call touched."""

    created: list[str] = []
    stored: list[tuple[str, str]] = []
    searched: list[tuple[str, str]] = []
    lock = threading.Lock()

    def __init__(self, db: Any, user_guid: str, **_: Any) -> None:
        self.user_guid = user_guid
        with self.lock:
            self.created.append(user_guid)

    def store(self, content: str, *_args: Any, **_: Any) -> int:
        with self.lock:
            self.stored.append((self.user_guid, content))
        return 1

    def search(self, query: str, **_: Any) -> list[dict[str, Any]]:
        with self.lock:
            self.searched.append((self.user_guid, query))
        return []


class RecordingAgentExecutor:
    """Stands in for AgentExecutor: records the user each sub-agent acts for."""

    users: list[tuple[str, str]] = []
    lock = threading.Lock()

    def __init__(self, llm: Any, db: Any, config: Any, **kwargs: Any) -> None:
        self.user_guid = kwargs["user_guid"]

    def execute(self, agent_id: str, agent_name: str, task: str, *args: Any, **_: Any) -> dict:
        with self.lock:
            self.users.append((self.user_guid, task))
        return {"content": "agent done", "status": "completed"}


class TurnLLM(StubLLMService):
    """Answers each user's first call with a memory store, a memory query and an agent,
    and every later call with plain text. The task names the user it was asked for."""

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        last = messages[-1]["content"]
        if isinstance(last, str) and last.startswith("for "):
            who = last[len("for ") :]
            calls = [
                {"type": "tool_use", "id": f"{who}-1", "name": "store_memory",
                 "input": {"content": f"{who} likes tea", "category": "preferences"}},
                {"type": "tool_use", "id": f"{who}-2", "name": "query_memory",
                 "input": {"query": f"what does {who} like"}},
                {"type": "tool_use", "id": f"{who}-3", "name": "spawn_agent",
                 "input": {"task": f"research for {who}", "agent_name": "Researcher"}},
            ]  # fmt: skip
            return {
                "content": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "tool_use": calls,
                "content_blocks": calls,
            }
        return {
            "content": "done",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": "done"}],
        }


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> None:
    import spark.core.agent_executor as agent_executor
    import spark.index.memory_index as memory_index

    RecordingMemoryIndex.created = []
    RecordingMemoryIndex.stored = []
    RecordingMemoryIndex.searched = []
    RecordingAgentExecutor.users = []
    monkeypatch.setattr(memory_index, "MemoryIndex", RecordingMemoryIndex)
    monkeypatch.setattr(agent_executor, "AgentExecutor", RecordingAgentExecutor)


def _manager(sync_db: Any, callback: Any) -> ConversationManager:
    llm = LLMManager()
    stub = TurnLLM()
    llm.register_provider(stub)  # type: ignore[arg-type]
    mgr = ConversationManager(
        sync_db,
        llm,
        ContextLimitResolver(),
        max_tool_iterations=4,
        user_guid=PROCESS,
        embedded_tools_config=AGENTS_ON,
        tool_permission_callback=callback,
    )
    mgr._get_llm_service_for_model = lambda model_id: stub  # type: ignore[method-assign]
    return mgr


def test_concurrent_turns_use_their_own_user(sync_db: Any, recorders: None) -> None:
    # Both turns must be inside their tool batch before either runs its agent,
    # so a guid kept on the instance would be the other turn's by then.
    barrier = threading.Barrier(2, timeout=20)

    def approve(tool_name: str, tool_input: dict) -> str:
        if tool_name == "query_memory":
            barrier.wait()
        return "once"

    mgr = _manager(sync_db, approve)
    conversations = {
        ADA: mgr.create_conversation("Ada's", "stub-model", ADA),
        GRACE: mgr.create_conversation("Grace's", "stub-model", GRACE),
    }
    errors: list[BaseException] = []

    def turn(user: str) -> None:
        try:
            mgr.send_message(conversations[user], f"for {user}", user)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=turn, args=(user,)) for user in (ADA, GRACE)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors

    assert sorted(RecordingMemoryIndex.stored) == [
        (ADA, f"{ADA} likes tea"),
        (GRACE, f"{GRACE} likes tea"),
    ]
    assert sorted(RecordingMemoryIndex.searched) == [
        (ADA, f"what does {ADA} like"),
        (GRACE, f"what does {GRACE} like"),
    ]
    assert PROCESS not in RecordingMemoryIndex.created
    assert sorted(RecordingAgentExecutor.users) == [
        (ADA, f"research for {ADA}"),
        (GRACE, f"research for {GRACE}"),
    ]


def test_a_turn_never_reads_another_users_conversation(sync_db: Any, recorders: None) -> None:
    mgr = _manager(sync_db, None)
    ada = mgr.create_conversation("Ada's", "stub-model", ADA)
    mgr.send_message(ada, "plain words", ADA)
    assert [m["content"] for m in mgr.get_messages(ada, ADA)] == ["plain words", "done"]
    assert mgr.get_messages(ada, GRACE) == []
    with pytest.raises(ValueError):
        mgr.send_message(ada, "plain words", GRACE)


def test_personal_mode_tools_fall_back_to_the_local_user(sync_db: Any, recorders: None) -> None:
    """A direct tool call without a turn's user (personal mode) acts for the local user."""
    mgr = _manager(sync_db, None)
    text, is_error = mgr._call_tool("store_memory", {"content": "local", "category": "facts"})
    assert not is_error, text
    assert RecordingMemoryIndex.stored == [(PROCESS, "local")]


def test_concurrent_turns_answer_only_their_own_prompts(sync_db: Any, recorders: None) -> None:
    """Each stream's approval callback hears only its own turn's tools."""
    from spark.core.conversation_manager import TurnCallbacks

    barrier = threading.Barrier(2, timeout=20)
    heard: dict[str, list[str]] = {ADA: [], GRACE: []}

    def approver(user: str) -> Any:
        def approve(tool_name: str, tool_input: dict) -> str:
            heard[user].append(str(tool_input.get("content") or tool_input.get("query") or ""))
            if tool_name == "store_memory":
                barrier.wait()
            return "once"

        return approve

    mgr = _manager(sync_db, None)
    conversations = {
        ADA: mgr.create_conversation("Ada's", "stub-model", ADA),
        GRACE: mgr.create_conversation("Grace's", "stub-model", GRACE),
    }
    errors: list[BaseException] = []

    def turn(user: str) -> None:
        try:
            mgr.send_message(
                conversations[user],
                f"for {user}",
                user,
                callbacks=TurnCallbacks(tool_permission=approver(user)),
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=turn, args=(user,)) for user in (ADA, GRACE)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    for user in (ADA, GRACE):
        assert heard[user][:2] == [f"{user} likes tea", f"what does {user} like"]
    assert mgr._tool_permission_callback is None
