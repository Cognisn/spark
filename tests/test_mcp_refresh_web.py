"""The per-server MCP tools refresh endpoint, in server mode (PostgreSQL) and personal mode.

Guards under test: only a holder of ``spark.settings.instance`` may refresh (401 and 403
otherwise, in server mode); the refresh replaces one server's tools and answers what
changed; a failing server answers an error and keeps its old tools; an unknown server
answers 404; a refresh is audited in server mode with the server and counts only.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.identity import audit
from spark.identity.permissions import POWER_USER, USER
from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from tests.identity_helpers import identity_for
from tests.test_mcp_refresh_manager import StubClient, _tool
from tests.test_mcp_servers_web import (  # noqa: F401 - fixtures
    _clean,
    config_file,
    personal,
    server,
)

PATH = "/settings/mcp/api/docs/tools/refresh"


def _attach(client: TestClient) -> None:
    StubClient.registry = {}
    manager = MCPManager(client_factory=StubClient)
    for name in ("docs", "wiki"):
        manager.add_server(
            MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
        )

    async def start() -> None:
        await manager.connect_all()
        await manager.list_all_tools()

    asyncio.run(start())
    client.app.state.mcp_manager = manager  # type: ignore[attr-defined]


def _exercise(client: TestClient) -> None:
    _attach(client)
    StubClient.registry["docs"].tools = [_tool("docs", "beta", "new")]
    resp = client.post(PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["name"] == "docs"
    assert body["added"] == ["beta"] and body["removed"] == ["alpha"] and body["changed"] == []
    assert body["tools"] == [{"name": "beta", "description": "new"}]
    # The other server is untouched.
    listed = client.get("/settings/mcp/api/wiki/tools").json()
    assert [t["name"] for t in listed["tools"]] == ["alpha"]
    assert [t["name"] for t in client.get("/settings/mcp/api/docs/tools").json()["tools"]] == [
        "beta"
    ]
    # A failing server keeps its tools and answers the error.
    StubClient.registry["docs"].fail = RuntimeError("upstream down")
    failed = client.post(PATH)
    assert failed.status_code == 502
    assert failed.json()["status"] == "failed"
    assert "upstream down" in failed.json()["error"]
    assert [t["name"] for t in failed.json()["tools"]] == ["beta"]
    # An unknown server is 404.
    assert client.post("/settings/mcp/api/missing/tools/refresh").status_code == 404


class TestPersonal:
    def test_refreshes_one_server(self, personal: TestClient) -> None:  # noqa: F811
        _exercise(personal)

    def test_without_a_manager_the_server_is_unknown(
        self, personal: TestClient
    ) -> None:  # noqa: F811
        personal.app.state.mcp_manager = None  # type: ignore[attr-defined]
        assert personal.post(PATH).status_code == 404

    def test_the_csrf_token_is_needed(self, personal: TestClient) -> None:  # noqa: F811
        from spark.web.security import CSRF_HEADER

        _attach(personal)
        token = personal.headers.pop(CSRF_HEADER)
        try:
            assert personal.post(PATH).status_code == 403
        finally:
            personal.headers[CSRF_HEADER] = token


@pytest.mark.postgres
class TestServer:
    def test_an_administrator_refreshes_one_server(self, server: Any) -> None:  # noqa: F811
        client, _, _ = server
        _exercise(client)

    @pytest.mark.parametrize("role", [USER, POWER_USER])
    def test_only_instance_administrators_may_refresh(
        self, server: Any, role: str  # noqa: F811
    ) -> None:
        client, holder, _ = server
        _attach(client)
        StubClient.registry["docs"].tools = [_tool("docs", "beta")]
        holder.identity = identity_for(role)
        assert client.post(PATH).status_code == 403
        assert StubClient.registry["docs"].list_calls == 1  # only the discovery in _attach

    def test_a_signed_out_caller_is_refused(self, server: Any) -> None:  # noqa: F811
        client, holder, _ = server
        _attach(client)
        holder.identity = None
        assert client.post(PATH).status_code == 401
        assert StubClient.registry["docs"].list_calls == 1

    def test_a_refresh_is_audited_without_tool_content(
        self, server: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
    ) -> None:
        client, _, _ = server
        _attach(client)
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        async def fake_emit(request: Any, action: str, *args: Any, **kw: Any) -> None:
            calls.append((action, args, kw))

        monkeypatch.setattr(audit, "emit_request", fake_emit)
        StubClient.registry["docs"].tools = [_tool("docs", "beta", "SECRET-DESCRIPTION")]
        assert client.post(PATH).status_code == 200
        StubClient.registry["docs"].fail = RuntimeError("upstream down")
        assert client.post(PATH).status_code == 502
        [(a1, p1, k1), (a2, p2, k2)] = calls
        assert a1 == a2 == audit.MCP_TOOLS_REFRESHED == "spark.mcp.tools.refreshed"
        assert p1 == ("success",) and p2 == ("failure",)
        assert (k1["resource_type"], k1["resource_id"]) == ("mcp_server", "docs")
        assert k1["details"] == {"server": "docs", "added": 1, "removed": 1, "changed": 0}
        assert "SECRET" not in repr(calls)

    def test_a_refused_or_unknown_refresh_is_not_audited(
        self, server: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
    ) -> None:
        client, holder, _ = server
        _attach(client)
        calls: list[str] = []

        async def fake_emit(request: Any, action: str, *args: Any, **kw: Any) -> None:
            calls.append(action)

        monkeypatch.setattr(audit, "emit_request", fake_emit)
        assert client.post("/settings/mcp/api/missing/tools/refresh").status_code == 404
        holder.identity = identity_for(USER)
        assert client.post(PATH).status_code == 403
        assert calls == []
