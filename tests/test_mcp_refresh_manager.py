"""Refreshing one MCP server's tools in the manager (no restart, no other server touched)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spark.mcp_integration.manager import MCPManager, MCPServerConfig


def _tool(server: str, name: str, description: str = "", schema: Any = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {},
        "server": server,
    }


class StubClient:
    """An MCP client whose tools are set by the test between calls."""

    registry: dict[str, StubClient] = {}

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self.connected = False
        self.tools: list[dict[str, Any]] = [_tool(config.name, "alpha")]
        self.fail: Exception | None = None
        self.hang = False
        self.connect_ok = True
        self.connects = 0
        self.list_calls = 0
        StubClient.registry[config.name] = self

    async def connect(self) -> bool:
        self.connects += 1
        self.connected = self.connect_ok
        return self.connect_ok

    async def disconnect(self) -> None:
        self.connected = False

    async def list_tools(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        if self.hang:
            await asyncio.sleep(60)
        if self.fail is not None:
            raise self.fail
        return [dict(t) for t in self.tools]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [], "isError": False}


async def _manager(*names: str) -> MCPManager:
    StubClient.registry = {}
    manager = MCPManager(client_factory=StubClient)
    for name in names:
        manager.add_server(
            MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
        )
    await manager.connect_all()
    await manager.list_all_tools()
    return manager


class TestRefreshServer:
    async def test_reports_added_removed_and_changed_tools_by_name(self) -> None:
        manager = await _manager("docs")
        StubClient.registry["docs"].tools = [
            _tool("docs", "beta"),
            _tool("docs", "gamma", "new"),
        ]
        first = await manager.refresh_server("docs")
        assert first.error is None
        assert first.added == ["beta", "gamma"]
        assert first.removed == ["alpha"]
        assert first.changed == []
        StubClient.registry["docs"].tools = [
            _tool("docs", "beta", "reworded"),
            _tool("docs", "gamma", "new", {"type": "object"}),
        ]
        second = await manager.refresh_server("docs")
        assert second.added == [] and second.removed == []
        assert second.changed == ["beta", "gamma"]

    async def test_the_next_listing_sees_the_new_tools(self) -> None:
        manager = await _manager("docs")
        StubClient.registry["docs"].tools = [_tool("docs", "beta")]
        await manager.refresh_server("docs")
        assert [t["name"] for t in await manager.list_all_tools()] == ["beta"]

    async def test_other_servers_are_not_listed_or_reconnected(self) -> None:
        manager = await _manager("docs", "wiki")
        other = StubClient.registry["wiki"]
        before_calls, before_connects = other.list_calls, other.connects
        StubClient.registry["docs"].tools = [_tool("docs", "beta")]
        await manager.refresh_server("docs")
        assert other.list_calls == before_calls
        assert other.connects == before_connects
        tools = await manager.list_all_tools()
        assert {(t["server"], t["name"]) for t in tools} == {("docs", "beta"), ("wiki", "alpha")}

    async def test_a_failing_server_keeps_its_old_list_and_reports_the_error(self) -> None:
        manager = await _manager("docs")
        client = StubClient.registry["docs"]
        client.fail = RuntimeError("upstream down")
        result = await manager.refresh_server("docs")
        assert result.error is not None and "upstream down" in result.error
        assert result.added == result.removed == result.changed == []
        assert [t["name"] for t in await manager.list_all_tools()] == ["alpha"]

    async def test_a_server_that_times_out_keeps_its_old_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.mcp_integration import manager as module

        monkeypatch.setattr(module, "REFRESH_TIMEOUT", 0.05)
        manager = await _manager("docs")
        StubClient.registry["docs"].hang = True
        result = await manager.refresh_server("docs")
        assert result.error is not None and "timed out" in result.error
        assert [t["name"] for t in await manager.list_all_tools()] == ["alpha"]

    async def test_a_dropped_server_is_reconnected_first(self) -> None:
        manager = await _manager("docs")
        client = StubClient.registry["docs"]
        client.connected = False
        client.tools = [_tool("docs", "beta")]
        result = await manager.refresh_server("docs")
        assert result.error is None
        assert client.connects == 2
        assert result.added == ["beta"] and result.removed == ["alpha"]

    async def test_a_server_that_cannot_reconnect_reports_the_error(self) -> None:
        manager = await _manager("docs")
        client = StubClient.registry["docs"]
        client.connected = False
        client.connect_ok = False
        result = await manager.refresh_server("docs")
        assert result.error is not None
        assert [t["name"] for t in await manager.list_all_tools()] == ["alpha"]

    async def test_an_unknown_server_is_refused(self) -> None:
        manager = await _manager("docs")
        with pytest.raises(KeyError):
            await manager.refresh_server("missing")

    async def test_refresh_before_any_discovery_fills_the_whole_cache(self) -> None:
        StubClient.registry = {}
        manager = MCPManager(client_factory=StubClient)
        for name in ("docs", "wiki"):
            manager.add_server(
                MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
            )
        await manager.connect_all()
        manager._tools_cache = None
        await manager.refresh_server("docs")
        assert {t["server"] for t in await manager.list_all_tools()} == {"docs", "wiki"}
