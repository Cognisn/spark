"""A server's chat turn: the model is offered only granted MCP tools, every call is audited
with its conversation, and the result still reaches the model as untrusted content (A12, A16).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from spark import mode
from spark.core.conversation_manager import ConversationManager
from spark.identity import audit
from spark.llm.context_limits import ContextLimitResolver
from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from spark.mcp_integration.runtime import MCPRuntime
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.test_mcp_manager_scoping import FakeClient

USER = "user-ada"
GRANTED = frozenset({"spark.mcp.search", "spark.tools.datetime"})


@pytest.fixture
def runtime() -> Iterator[MCPRuntime]:
    FakeClient.instances = []
    manager = MCPManager(client_factory=FakeClient)
    for name in ("search", "finance"):
        manager.add_server(
            MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
        )
    rt = MCPRuntime.start(manager)
    rt.run(manager.connect_all())
    yield rt
    rt.stop()


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        audit, "_current", SimpleNamespace(submit=lambda *a, **k: seen.append((a, k)))
    )
    return seen


@pytest.fixture
def manager(sync_db: Any, runtime: MCPRuntime) -> Iterator[ConversationManager]:
    cm = ConversationManager(
        sync_db,
        MagicMock(),
        ContextLimitResolver(),
        mcp_manager=runtime.manager,
        mcp_loop=runtime.loop,
    )
    # Server mode from here on (the database was opened in personal mode as a fixture).
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield cm
    mode.reset()


def _mcp_names(tools: list[dict[str, Any]]) -> set[str]:
    return {t["name"] for t in tools if t["name"].endswith("_lookup")}


class TestToolList:
    def test_the_model_is_offered_only_granted_servers(self, manager: ConversationManager) -> None:
        assert _mcp_names(manager._get_all_tools(GRANTED)) == {"search_lookup"}

    def test_without_permissions_no_mcp_tool_is_offered(self, manager: ConversationManager) -> None:
        assert _mcp_names(manager._get_all_tools(None)) == set()


class TestCalls:
    def test_a_granted_call_is_audited_with_its_conversation(
        self, manager: ConversationManager, events: list[Any]
    ) -> None:
        text, is_error = manager._call_tool(
            "search_lookup", {"q": "x"}, permissions=GRANTED, conversation_id=42, user_guid=USER
        )
        assert (text, is_error) == ("ok", False)
        (action, outcome), fields = events[-1]
        assert (action, outcome) == (audit.MCP_TOOL_CALLED, "success")
        assert fields["user_guid"] == USER
        assert fields["details"] == {
            "server": "search",
            "tool": "search_lookup",
            "conversation_id": 42,
            "result": "success",
        }

    def test_a_server_not_granted_is_refused_and_audited(
        self, manager: ConversationManager, events: list[Any]
    ) -> None:
        text, is_error = manager._call_tool(
            "finance_lookup", {}, permissions=GRANTED, conversation_id=42, user_guid=USER
        )
        assert is_error is True and "not available" in text
        assert not FakeClient.instances[1].calls
        (action, outcome), fields = events[-1]
        assert (action, outcome) == (audit.MCP_TOOL_REFUSED, "failure")
        assert fields["details"]["conversation_id"] == 42

    def test_the_result_reaches_the_model_as_untrusted_content(
        self, manager: ConversationManager, sync_db: Any, events: list[Any]
    ) -> None:
        from spark.database import conversations

        cid = sync_db.call(conversations.create_conversation, "Tools", "m", USER)
        result = manager._execute_single_tool(
            {"id": "t1", "name": "search_lookup", "input": {}},
            cid,
            USER,
            permissions=GRANTED,
        )
        assert "untrusted-content" in str(result["content"])
        assert any(k["details"]["conversation_id"] == cid for _, k in events)
