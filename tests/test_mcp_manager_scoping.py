"""The MCP manager in server mode: per-user tools, refusals and the audit trail (A12).

A server lists and calls only the MCP servers the caller's permissions grant
(``spark.mcp.<server>``), and fails closed when no permissions are given. Every
tool call is audited with user, conversation, server, tool and outcome,
including failures and refusals. Personal mode is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.identity import audit
from spark.mcp_integration.manager import MCPManager, MCPServerConfig, MCPToolRefused
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings


class FakeClient:
    """An MCP client with fixed tools; ``fail`` makes calls raise."""

    instances: list[FakeClient] = []

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self.connected = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False
        self.is_error = False
        self.disconnects = 0
        FakeClient.instances.append(self)

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{self.name}_lookup",
                "description": "",
                "inputSchema": {},
                "server": self.name,
            },
            {"name": "shared", "description": "", "inputSchema": {}, "server": self.name},
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if self.fail:
            raise RuntimeError("upstream down")
        return {"content": [{"type": "text", "text": "ok"}], "isError": self.is_error}


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def submit(self, action: str, outcome: str = "success", **fields: Any) -> None:
        self.events.append((action, outcome, fields))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> FakeAudit:
    fake = FakeAudit()
    monkeypatch.setattr(audit, "_current", fake)
    return fake


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


async def _manager(*names: str) -> MCPManager:
    FakeClient.instances = []
    manager = MCPManager(client_factory=FakeClient)
    for name in names:
        manager.add_server(
            MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
        )
    await manager.connect_all()
    return manager


GRANT_SEARCH = frozenset({"spark.mcp.search", "spark.tools.web"})


# -- Listing -------------------------------------------------------------------


class TestListing:
    async def test_server_lists_only_granted_servers_tools(self, server_mode: None) -> None:
        manager = await _manager("search", "finance")
        tools = await manager.list_all_tools(permissions=GRANT_SEARCH)
        assert {t["server"] for t in tools} == {"search"}
        assert {t["name"] for t in tools} == {"search_lookup", "shared"}

    async def test_server_unscoped_listing_fails_closed(self, server_mode: None) -> None:
        manager = await _manager("search")
        assert await manager.list_all_tools() == []
        assert await manager.list_all_tools(permissions=None) == []

    async def test_server_lists_nothing_without_a_grant(self, server_mode: None) -> None:
        manager = await _manager("search")
        assert await manager.list_all_tools(permissions=frozenset({"spark.tools.web"})) == []

    async def test_server_cached_tools_and_servers_are_filtered(self, server_mode: None) -> None:
        manager = await _manager("search", "finance")
        await manager.list_all_tools(permissions=GRANT_SEARCH)
        assert {t["server"] for t in manager.cached_tools(permissions=GRANT_SEARCH)} == {"search"}
        assert set(manager.servers_for(GRANT_SEARCH)) == {"search"}
        assert manager.cached_tools(permissions=None) == []
        assert manager.servers_for(None) == {}

    async def test_personal_mode_lists_everything(self) -> None:
        manager = await _manager("search", "finance")
        assert {t["server"] for t in await manager.list_all_tools()} == {"search", "finance"}
        assert {t["server"] for t in await manager.list_all_tools(permissions=None)} == {
            "search",
            "finance",
        }
        assert set(manager.servers_for(None)) == {"search", "finance"}


# -- Calling -------------------------------------------------------------------


class TestCalling:
    async def test_server_calls_a_granted_server(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search")
        result = await manager.call_tool(
            "search_lookup",
            {"q": "x"},
            permissions=GRANT_SEARCH,
            user_guid="u-1",
            conversation_id=7,
        )
        assert result["isError"] is False
        assert FakeClient.instances[0].calls == [("search_lookup", {"q": "x"})]

    async def test_server_refuses_a_server_not_granted(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search", "finance")
        with pytest.raises(MCPToolRefused):
            await manager.call_tool(
                "finance_lookup", {}, permissions=GRANT_SEARCH, user_guid="u-1", conversation_id=7
            )
        assert all(not c.calls for c in FakeClient.instances)

    async def test_server_refuses_an_unscoped_call(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search")
        with pytest.raises(MCPToolRefused):
            await manager.call_tool("search_lookup", {})
        assert not FakeClient.instances[0].calls

    async def test_server_refuses_a_named_server_not_granted(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search", "finance")
        with pytest.raises(MCPToolRefused):
            await manager.call_tool(
                "shared", {}, server_name="finance", permissions=GRANT_SEARCH, user_guid="u-1"
            )

    async def test_shared_tool_name_resolves_to_the_granted_server(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("finance", "search")
        await manager.call_tool("shared", {}, permissions=GRANT_SEARCH, user_guid="u-1")
        finance, search = FakeClient.instances
        assert not finance.calls and search.calls == [("shared", {})]

    async def test_personal_mode_calls_any_server(self, recorder: FakeAudit) -> None:
        manager = await _manager("search", "finance")
        await manager.call_tool("finance_lookup", {})
        assert FakeClient.instances[1].calls == [("finance_lookup", {})]


# -- Audit ---------------------------------------------------------------------


class TestAudit:
    async def test_success_is_audited(self, server_mode: None, recorder: FakeAudit) -> None:
        manager = await _manager("search")
        await manager.call_tool(
            "search_lookup",
            {"q": "secret"},
            permissions=GRANT_SEARCH,
            user_guid="u-1",
            conversation_id=7,
        )
        assert recorder.events == [
            (
                audit.MCP_TOOL_CALLED,
                "success",
                {
                    "user_guid": "u-1",
                    "resource_type": "mcp_tool",
                    "resource_id": "search/search_lookup",
                    "details": {
                        "server": "search",
                        "tool": "search_lookup",
                        "conversation_id": 7,
                        "result": "success",
                    },
                },
            )
        ]

    async def test_tool_error_is_audited_as_failure(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search")
        FakeClient.instances[0].is_error = True
        await manager.call_tool("search_lookup", {}, permissions=GRANT_SEARCH, user_guid="u-1")
        action, outcome, fields = recorder.events[-1]
        assert (action, outcome, fields["details"]["result"]) == (
            audit.MCP_TOOL_CALLED,
            "failure",
            "error",
        )

    async def test_exception_is_audited_and_raised(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search")
        FakeClient.instances[0].fail = True
        with pytest.raises(RuntimeError):
            await manager.call_tool("search_lookup", {}, permissions=GRANT_SEARCH, user_guid="u-1")
        action, outcome, fields = recorder.events[-1]
        assert (action, outcome, fields["details"]["result"]) == (
            audit.MCP_TOOL_CALLED,
            "failure",
            "failed",
        )

    async def test_refusal_is_audited(self, server_mode: None, recorder: FakeAudit) -> None:
        manager = await _manager("search", "finance")
        with pytest.raises(MCPToolRefused):
            await manager.call_tool(
                "finance_lookup", {}, permissions=GRANT_SEARCH, user_guid="u-1", conversation_id=3
            )
        assert recorder.events == [
            (
                audit.MCP_TOOL_REFUSED,
                "failure",
                {
                    "user_guid": "u-1",
                    "resource_type": "mcp_tool",
                    "resource_id": "finance/finance_lookup",
                    "details": {
                        "server": "finance",
                        "tool": "finance_lookup",
                        "conversation_id": 3,
                        "result": "refused",
                    },
                },
            )
        ]

    async def test_unscoped_refusal_is_audited(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search")
        with pytest.raises(MCPToolRefused):
            await manager.call_tool("search_lookup", {})
        assert [e[0] for e in recorder.events] == [audit.MCP_TOOL_REFUSED]

    async def test_unknown_tool_is_audited(self, server_mode: None, recorder: FakeAudit) -> None:
        manager = await _manager("search")
        with pytest.raises(ValueError):
            await manager.call_tool("nowhere", {}, permissions=GRANT_SEARCH, user_guid="u-1")
        action, outcome, fields = recorder.events[-1]
        assert (action, outcome, fields["details"]["result"], fields["details"]["server"]) == (
            audit.MCP_TOOL_CALLED,
            "failure",
            "unavailable",
            None,
        )

    async def test_disconnected_server_is_audited(
        self, server_mode: None, recorder: FakeAudit
    ) -> None:
        manager = await _manager("search")
        await manager.list_all_tools(permissions=GRANT_SEARCH)
        FakeClient.instances[0].connected = False
        with pytest.raises(RuntimeError):
            await manager.call_tool(
                "search_lookup", {}, server_name="search", permissions=GRANT_SEARCH, user_guid="u"
            )
        assert recorder.events[-1][2]["details"]["result"] == "unavailable"

    def test_actions_are_registered(self) -> None:
        assert audit.MCP_TOOL_CALLED in audit.ACTIONS
        assert audit.MCP_TOOL_REFUSED in audit.ACTIONS
        assert audit.MCP_TOOL_CALLED.startswith("spark.")


# -- Reconnecting after a change -----------------------------------------------


class TestReconcile:
    async def test_adds_changes_and_removes_servers(self) -> None:
        manager = await _manager("search", "finance")
        search, finance = FakeClient.instances
        changed = await manager.reconcile(
            [
                MCPServerConfig(
                    name="search", transport="http", url="https://search.example.com/mcp"
                ),
                MCPServerConfig(
                    name="finance", transport="http", url="https://finance.example.com/v2"
                ),
                MCPServerConfig(name="kb", transport="sse", url="https://kb.example.com/sse"),
            ]
        )
        assert changed == {"finance", "kb"}
        assert search.disconnects == 0
        assert finance.disconnects == 1
        assert set(manager.servers) == {"search", "finance", "kb"}
        assert all(c.connected for c in manager.servers.values())

        changed = await manager.reconcile(
            [MCPServerConfig(name="kb", transport="sse", url="https://kb.example.com/sse")]
        )
        assert changed == {"search", "finance"}
        assert set(manager.servers) == {"kb"}
        assert search.disconnects == 1

    async def test_disabled_server_is_disconnected(self) -> None:
        manager = await _manager("search")
        await manager.reconcile(
            [
                MCPServerConfig(
                    name="search",
                    transport="http",
                    url="https://search.example.com/mcp",
                    enabled=False,
                )
            ]
        )
        assert manager.servers == {}
        assert FakeClient.instances[0].disconnects == 1

    async def test_tool_cache_is_rebuilt(self) -> None:
        manager = await _manager("search")
        await manager.list_all_tools()
        await manager.reconcile(
            [MCPServerConfig(name="kb", transport="sse", url="https://kb.example.com/sse")]
        )
        assert {t["server"] for t in manager.cached_tools(permissions=None)} == {"kb"}
