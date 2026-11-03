"""HTTP and SSE MCP servers, end to end against a local stub server (127.0.0.1).

A server allows only these two transports (A12), so both must work with the
installed MCP SDK (2.x renamed its client and its result fields; 1.x is still
accepted): connect with credentials, list tools with their input schemas, call
a tool, and report a tool's own error as an error.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from spark.mcp_integration.manager import MCPClient, MCPManager, MCPServerConfig, MCPToolRefused
from tests.mcp_stub_server import StubServer, serving


@pytest.fixture(scope="module")
def stub() -> Iterator[StubServer]:
    with serving() as server:
        yield server


def _url(stub: StubServer, transport: str) -> str:
    return stub.http_url if transport == "http" else stub.sse_url


@pytest.mark.parametrize("transport", ["http", "sse"])
class TestTransport:
    async def test_lists_tools_with_schemas(self, stub: StubServer, transport: str) -> None:
        client = MCPClient(
            MCPServerConfig(name="stub", transport=transport, url=_url(stub, transport), timeout=15)
        )
        assert await client.connect()
        try:
            tools = {t["name"]: t for t in await client.list_tools()}
        finally:
            await client.disconnect()
        assert set(tools) == {"echo", "fail"}
        assert "text" in tools["echo"]["inputSchema"]["properties"]
        assert tools["echo"]["server"] == "stub"

    async def test_calls_a_tool(self, stub: StubServer, transport: str) -> None:
        client = MCPClient(
            MCPServerConfig(name="stub", transport=transport, url=_url(stub, transport), timeout=15)
        )
        assert await client.connect()
        try:
            ok = await client.call_tool("echo", {"text": "hello"})
            failed = await client.call_tool("fail", {})
        finally:
            await client.disconnect()
        assert ok == {"content": [{"type": "text", "text": "echo: hello"}], "isError": False}
        assert failed["isError"] is True

    async def test_sends_credentials(self, stub: StubServer, transport: str) -> None:
        stub.headers.clear()
        client = MCPClient(
            MCPServerConfig(
                name="stub",
                transport=transport,
                url=_url(stub, transport),
                auth_type="bearer",
                auth_token="example-token",
                custom_headers={"X-Key": "example-key"},
                timeout=15,
            )
        )
        assert await client.connect()
        await client.disconnect()
        assert {"authorization": "Bearer example-token", "x-key": "example-key"} in stub.headers

    async def test_unreachable_server_does_not_connect(self, transport: str) -> None:
        client = MCPClient(
            MCPServerConfig(
                name="gone", transport=transport, url="http://127.0.0.1:9/mcp", timeout=3
            )
        )
        assert await client.connect() is False


class TestServerModeEndToEnd:
    async def test_granted_server_is_called_and_other_refused(
        self, stub: StubServer, server_mode_configured: None
    ) -> None:
        manager = MCPManager()
        manager.add_server(MCPServerConfig(name="search", transport="http", url=stub.http_url))
        manager.add_server(MCPServerConfig(name="finance", transport="sse", url=stub.sse_url))
        await manager.connect_all()
        try:
            granted = frozenset({"spark.mcp.search"})
            tools = await manager.list_all_tools(permissions=granted)
            assert {t["server"] for t in tools} == {"search"}
            result = await manager.call_tool(
                "echo", {"text": "x"}, permissions=granted, user_guid="u-1"
            )
            assert result["content"][0]["text"] == "echo: x"
            with pytest.raises(MCPToolRefused):
                await manager.call_tool(
                    "echo", {"text": "x"}, server_name="finance", permissions=granted
                )
        finally:
            await manager.disconnect_all()


@pytest.fixture
def server_mode_configured() -> Iterator[None]:
    from spark import mode
    from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()
