"""A turn in a shared conversation: the sender's throughout, and nothing personal in it.

CS28: a turn runs as its sender, and personal memories are left out of a shared
conversation. While a conversation has any member besides its owner, every turn
(the owner's too) is offered no memory or knowledge graph tool, has such a call
refused however it was asked for, searches no linked conversation and gets no
knowledge graph context.
Unshared, nothing changes. Each check was mutation-checked by turning ``shared``
off in the conversation manager: the shared cases then fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark.core.conversation_manager import PRIVATE_TOOLS, ConversationManager
from spark.database import conversation_links, conversation_members, messages
from spark.database.scope import NotOwned, NotPermitted, Role
from tests.model_helpers import FakeModels
from tests.test_conversation_manager import StubLLMService
from tests.test_conversation_manager_users import RecordingMemoryIndex

ADA = "user-ada-example"
CORA = "user-cora-example"
VIC = "user-vic-example"


class ToolsLLM(StubLLMService):
    """Records the tools and system prompt of every call; can answer with a memory call."""

    def __init__(self) -> None:
        super().__init__()
        self._seen["tools"] = []

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        self._seen["tools"].append([t["name"] for t in kwargs.get("tools") or []])
        return super().invoke_model(messages, **kwargs)

    @property
    def offered(self) -> list[list[str]]:
        return list(self._seen["tools"])


def _memory_call() -> dict[str, Any]:
    call = {
        "type": "tool_use",
        "id": "m1",
        "name": "store_memory",
        "input": {"content": "Ada likes tea", "category": "preferences"},
    }
    return {
        "content": "",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "tool_use": [call],
        "content_blocks": [call],
    }


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> None:
    import spark.index.memory_index as memory_index

    RecordingMemoryIndex.created = []
    RecordingMemoryIndex.stored = []
    RecordingMemoryIndex.searched = []
    monkeypatch.setattr(memory_index, "MemoryIndex", RecordingMemoryIndex)


def _manager(sync_db: Any) -> tuple[ConversationManager, ToolsLLM]:
    stub = ToolsLLM()
    mgr = ConversationManager(sync_db, FakeModels(stub), max_tool_iterations=3)
    return mgr, stub


def _conversation(sync_db: Any, mgr: ConversationManager, *, shared: bool) -> int:
    cid = mgr.create_conversation("Ada's", "stub-model", ADA)
    sync_db.execute("UPDATE conversations SET rag_enabled = 0 WHERE id = ?", (cid,))
    if shared:
        sync_db.call(conversation_members.share, ADA, cid, CORA, Role.CONTRIBUTOR)
        sync_db.call(conversation_members.share, ADA, cid, VIC, Role.VIEWER)
    return cid


@pytest.mark.parametrize("sender", [ADA, CORA])
def test_a_shared_turn_offers_no_memory_tool(sync_db: Any, recorders: None, sender: str) -> None:
    mgr, stub = _manager(sync_db)
    cid = _conversation(sync_db, mgr, shared=True)
    mgr.send_message(cid, "hello", sender)
    assert stub.offered and not (set(stub.offered[0]) & PRIVATE_TOOLS)
    assert "Shared conversation" in str(stub.last_system)
    assert "store_memory" not in str(stub.last_system)


def test_an_unshared_turn_keeps_its_memory_tools(sync_db: Any, recorders: None) -> None:
    mgr, stub = _manager(sync_db)
    cid = _conversation(sync_db, mgr, shared=False)
    mgr.send_message(cid, "hello", ADA)
    assert PRIVATE_TOOLS <= set(stub.offered[0])
    assert "Shared conversation" not in str(stub.last_system)


def test_a_memory_call_in_a_shared_turn_is_refused(sync_db: Any, recorders: None) -> None:
    mgr, stub = _manager(sync_db)
    cid = _conversation(sync_db, mgr, shared=True)
    stub.responses = [_memory_call()]
    mgr.send_message(cid, "remember this", CORA)
    assert RecordingMemoryIndex.stored == []
    text, is_error = mgr._call_tool(
        "store_memory", {"content": "x"}, conversation_id=cid, user_guid=ADA
    )
    assert is_error and "shared conversation" in text
    assert RecordingMemoryIndex.stored == []
    text, is_error = mgr._call_tool(
        "query_knowledge_graph", {"query": "tea"}, conversation_id=cid, user_guid=CORA
    )
    assert is_error and "knowledge graphs are not available" in text


def test_the_turn_is_the_senders(sync_db: Any, recorders: None) -> None:
    mgr, _ = _manager(sync_db)
    cid = _conversation(sync_db, mgr, shared=True)
    mgr.send_message(cid, "from cora", CORA)
    rows = sync_db.call(messages.get_messages, VIC, cid)
    assert [(m["role"], m["user_guid"]) for m in rows] == [("user", CORA), ("assistant", CORA)]


def test_a_viewer_cannot_send(sync_db: Any, recorders: None) -> None:
    mgr, _ = _manager(sync_db)
    cid = _conversation(sync_db, mgr, shared=True)
    with pytest.raises(NotPermitted):
        mgr.send_message(cid, "from vic", VIC)
    with pytest.raises((NotOwned, ValueError)):
        mgr.send_message(cid, "from a stranger", "user-sam-example")
    assert sync_db.call(messages.get_messages, ADA, cid) == []


class _Index:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def search(self, query: str, **_: Any) -> list[dict]:
        self.calls.append(("search", None))
        return []

    def search_multi(self, query: str, ids: list[int], **_: Any) -> list[dict]:
        self.calls.append(("search_multi", list(ids)))
        return []


@pytest.mark.parametrize("shared", [True, False])
def test_linked_conversations_are_not_searched_while_shared(sync_db: Any, shared: bool) -> None:
    mgr, _ = _manager(sync_db)
    cid = _conversation(sync_db, mgr, shared=False)
    other = mgr.create_conversation("Ada's other", "stub-model", ADA)
    assert sync_db.call(conversation_links.add_link, cid, other, ADA)
    index = _Index()
    mgr._get_vector_index = lambda *_a, **_k: index  # type: ignore[method-assign]
    mgr._retrieve_relevant_context(cid, ADA, "tea", {"rag_enabled": 1}, shared=shared)
    if shared:
        assert index.calls == [("search", None)]
    else:
        assert index.calls == [("search_multi", [cid, other])]


def test_no_knowledge_graph_context_while_shared(
    sync_db: Any, recorders: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spark.knowledge.resolve as resolve

    asked: list[int] = []

    async def scopes(db: Any, conversation_id: int | None, user_guid: str) -> list[str]:
        asked.append(int(conversation_id or 0))
        return []

    monkeypatch.setattr(resolve, "available_scopes", scopes)
    mgr, _ = _manager(sync_db)
    shared = _conversation(sync_db, mgr, shared=True)
    alone = _conversation(sync_db, mgr, shared=False)
    mgr.send_message(shared, "hello", ADA)
    mgr.send_message(alone, "hello", ADA)
    assert asked == [alone]


def test_server_permissions_lose_the_private_categories() -> None:
    from spark.core.conversation_manager import _without_private
    from spark.tools.registry import ANY_ROLE, permission_name

    held = frozenset({permission_name(c) for c in ("memory", "knowledge", "web")})
    assert _without_private(held) == frozenset({permission_name("web")})
    assert _without_private(ANY_ROLE) is ANY_ROLE
    assert _without_private(None) is None


def test_model_refusal_is_none_in_personal_mode(sync_db: Any) -> None:
    mgr, _ = _manager(sync_db)
    assert mgr.model_refusal("stub-model", CORA, (), None) is None
