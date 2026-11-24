"""Tests for the MCP integration module."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spark.mcp_integration.manager import MCPClient, MCPManager, MCPServerConfig

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
