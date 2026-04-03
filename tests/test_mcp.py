"""Tests for the MCP integration module."""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spark.mcp_integration.manager import MCPClient, MCPManager, MCPServerConfig
from spark.mcp_integration.tool_selector import ToolSelector

# -- MCPServerConfig ----------------------------------------------------------


class TestMCPServerConfig:
    def test_defaults(self) -> None:
        cfg = MCPServerConfig(name="test")
        assert cfg.transport == "stdio"
        assert cfg.enabled is True
        assert cfg.auth_type == "none"
        assert cfg.timeout == pytest.approx(30.0)
        assert cfg.ssl_verify is True

    def test_custom(self) -> None:
        cfg = MCPServerConfig(
            name="custom",
            transport="http",
            url="https://example.com/mcp",
            auth_type="bearer",
            auth_token="tok123",
            timeout=60.0,
            ssl_verify=False,
        )
        assert cfg.transport == "http"
        assert cfg.url == "https://example.com/mcp"
        assert cfg.auth_token == "tok123"


# -- MCPClient auth headers --------------------------------------------------


class TestMCPClientAuth:
    def test_no_auth(self) -> None:
        client = MCPClient(MCPServerConfig(name="t", auth_type="none"))
        assert client._build_auth_headers() == {}

    def test_bearer(self) -> None:
        client = MCPClient(MCPServerConfig(name="t", auth_type="bearer", auth_token="mytoken"))
        headers = client._build_auth_headers()
        assert headers["Authorization"] == "Bearer mytoken"

    def test_api_key_default_header(self) -> None:
        client = MCPClient(MCPServerConfig(name="t", auth_type="api_key", auth_token="key123"))
        headers = client._build_auth_headers()
        assert headers["X-API-Key"] == "key123"

    def test_api_key_custom_header(self) -> None:
        client = MCPClient(
            MCPServerConfig(
                name="t", auth_type="api_key", auth_token="key123", auth_header_name="X-Custom"
            )
        )
        headers = client._build_auth_headers()
        assert headers["X-Custom"] == "key123"

    def test_basic(self) -> None:
        test_secret = "test-cred"  # noqa: S105
        client = MCPClient(
            MCPServerConfig(
                name="t",
                auth_type="basic",
                basic_username="user",
                basic_password=test_secret,
            )
        )
        headers = client._build_auth_headers()
        expected = base64.b64encode(b"user:test-cred").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_basic_no_password(self) -> None:
        client = MCPClient(MCPServerConfig(name="t", auth_type="basic", basic_username="user"))
        headers = client._build_auth_headers()
        expected = base64.b64encode(b"user:").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_custom_headers(self) -> None:
        client = MCPClient(
            MCPServerConfig(
                name="t", auth_type="custom", custom_headers={"X-Foo": "bar", "X-Baz": "qux"}
            )
        )
        headers = client._build_auth_headers()
        assert headers == {"X-Foo": "bar", "X-Baz": "qux"}


class TestMCPClientProperties:
    def test_name(self) -> None:
        client = MCPClient(MCPServerConfig(name="my-server"))
        assert client.name == "my-server"

    def test_not_connected_initially(self) -> None:
        client = MCPClient(MCPServerConfig(name="t"))
        assert client.connected is False

    def test_config_accessible(self) -> None:
        cfg = MCPServerConfig(name="t", timeout=99.0)
        client = MCPClient(cfg)
        assert client.config.timeout == pytest.approx(99.0)


# -- MCPManager ---------------------------------------------------------------


class TestMCPManager:
    def test_add_server(self) -> None:
        mgr = MCPManager()
        mgr.add_server(MCPServerConfig(name="s1"))
        assert "s1" in mgr.servers

    def test_skip_disabled(self) -> None:
        mgr = MCPManager()
        mgr.add_server(MCPServerConfig(name="s1", enabled=False))
        assert "s1" not in mgr.servers

    def test_get_server_timeout(self) -> None:
        mgr = MCPManager()
        mgr.add_server(MCPServerConfig(name="s1", timeout=45.0))
        assert mgr.get_server_timeout(server_name="s1") == pytest.approx(45.0)

    def test_get_server_timeout_default(self) -> None:
        mgr = MCPManager()
        assert mgr.get_server_timeout(server_name="unknown") == pytest.approx(30.0)

    def test_get_server_timeout_by_tool(self) -> None:
        mgr = MCPManager()
        mgr.add_server(MCPServerConfig(name="s1", timeout=99.0))
        # Manually populate cache
        mgr._tools_cache = [{"name": "my_tool", "server": "s1"}]
        assert mgr.get_server_timeout(tool_name="my_tool") == pytest.approx(99.0)

    def test_invalidate_cache(self) -> None:
        mgr = MCPManager()
        mgr._tools_cache = [{"name": "cached"}]
        mgr.invalidate_cache()
        assert mgr._tools_cache is None

    def test_from_config_empty(self) -> None:
        mgr = MCPManager.from_config({})
        assert len(mgr.servers) == 0

    def test_from_config(self) -> None:
        config = {
            "mcp": {
                "servers": [
                    {"name": "srv1", "transport": "stdio", "command": "npx", "args": ["-y", "pkg"]},
                    {
                        "name": "srv2",
                        "transport": "http",
                        "url": "http://localhost:8000",
                        "enabled": False,
                    },
                ]
            }
        }
        mgr = MCPManager.from_config(config)
        assert "srv1" in mgr.servers
        assert "srv2" not in mgr.servers  # disabled

    def test_from_config_auth(self) -> None:
        config = {
            "mcp": {
                "servers": [
                    {
                        "name": "authed",
                        "transport": "http",
                        "url": "https://example.com",
                        "auth_type": "bearer",
                        "auth_token": "secret",
                        "timeout": 60,
                        "ssl_verify": False,
                    }
                ]
            }
        }
        mgr = MCPManager.from_config(config)
        client = mgr.servers["authed"]
        assert client.config.auth_type == "bearer"
        assert client.config.auth_token == "secret"
        assert client.config.timeout == pytest.approx(60.0)
        assert client.config.ssl_verify is False


class TestMCPManagerAsync:
    @pytest.mark.asyncio
    async def test_list_all_tools_no_servers(self) -> None:
        mgr = MCPManager()
        tools = await mgr.list_all_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self) -> None:
        mgr = MCPManager()
        mgr._tools_cache = []
        with pytest.raises(ValueError, match="not found"):
            await mgr.call_tool("nonexistent", {})

    @pytest.mark.asyncio
    async def test_call_tool_server_not_connected(self) -> None:
        mgr = MCPManager()
        mgr.add_server(MCPServerConfig(name="srv"))
        with pytest.raises(RuntimeError, match="not connected"):
            await mgr.call_tool("tool", {}, server_name="srv")


# -- ToolSelector -------------------------------------------------------------


class TestToolSelector:
    def _make_tools(self, names: list[str]) -> list[dict[str, Any]]:
        return [{"name": n, "description": f"Tool {n}"} for n in names]

    def test_returns_all_when_under_limit(self) -> None:
        selector = ToolSelector(max_tools=10)
        tools = self._make_tools(["a", "b", "c"])
        result = selector.select_tools(tools, "hello")
        assert len(result) == 3

    def test_limits_output(self) -> None:
        selector = ToolSelector(max_tools=3)
        tools = self._make_tools([f"tool_{i}" for i in range(20)])
        result = selector.select_tools(tools, "hello")
        assert len(result) == 3

    def test_detects_filesystem_category(self) -> None:
        selector = ToolSelector(max_tools=5)
        tools = self._make_tools(
            [
                "read_file",
                "web_search",
                "get_current_datetime",
                "unrelated_1",
                "unrelated_2",
                "unrelated_3",
                "unrelated_4",
                "unrelated_5",
                "unrelated_6",
            ]
        )
        result = selector.select_tools(tools, "Please read the file at /tmp/test.txt")
        names = {t["name"] for t in result}
        assert "read_file" in names

    def test_detects_web_category(self) -> None:
        selector = ToolSelector(max_tools=5)
        tools = self._make_tools(
            [
                "read_file",
                "web_search",
                "fetch_url",
                "unrelated_1",
                "unrelated_2",
                "unrelated_3",
                "unrelated_4",
                "unrelated_5",
            ]
        )
        result = selector.select_tools(tools, "search the web for python tutorials")
        names = {t["name"] for t in result}
        assert "web_search" in names

    def test_detects_datetime_category(self) -> None:
        selector = ToolSelector(max_tools=5)
        tools = self._make_tools(
            [
                "get_current_datetime",
                "read_file",
                "other1",
                "other2",
                "other3",
                "other4",
                "other5",
                "other6",
            ]
        )
        result = selector.select_tools(tools, "what time is it now?")
        names = {t["name"] for t in result}
        assert "get_current_datetime" in names

    def test_uses_history_for_detection(self) -> None:
        selector = ToolSelector(max_tools=5)
        tools = self._make_tools(
            ["read_file", "web_search", "other1", "other2", "other3", "other4", "other5", "other6"]
        )
        history = [{"role": "user", "content": "I need to read some files"}]
        result = selector.select_tools(tools, "please continue", history)
        names = {t["name"] for t in result}
        assert "read_file" in names

    def test_no_categories_still_returns_tools(self) -> None:
        selector = ToolSelector(max_tools=5)
        tools = self._make_tools(["a", "b", "c", "d", "e", "f", "g"])
        result = selector.select_tools(tools, "hello there")
        assert len(result) == 5

    def test_always_includes_datetime(self) -> None:
        selector = ToolSelector(max_tools=3)
        tools = self._make_tools(["get_current_datetime", "a", "b", "c", "d", "e"])
        result = selector.select_tools(tools, "do something unrelated to time")
        names = {t["name"] for t in result}
        assert "get_current_datetime" in names
