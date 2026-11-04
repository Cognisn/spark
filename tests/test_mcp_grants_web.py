"""Server mode, signed in through warden: users see only the MCP servers granted to their roles.

The grants are the ``roles`` of each ``mcp.servers`` entry (A12). The web
process adds the granted ``spark.mcp.<server>`` permissions to each signed-in
request (``spark.mcp_integration.access``), so the conversation tool panel, the
main menu and the actions tool list show only granted servers, and the settings
API stays an administrator's.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations
from spark.identity.permissions import ADMINISTRATOR, AUDITOR, POWER_USER, USER
from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from tests.test_mcp_manager_scoping import FakeClient
from tests.test_user_scoping_web import (  # noqa: F401 - fixtures
    _call,
    _signed_in,
    client,
    host,
    server_db,
)

pytestmark = pytest.mark.postgres

SERVERS = [
    {"name": "search", "transport": "http", "url": "https://s.example.com/mcp", "roles": [USER]},
    {
        "name": "finance",
        "transport": "sse",
        "url": "https://f.example.com/sse",
        "roles": [POWER_USER],
    },
    {"name": "spare", "transport": "http", "url": "https://x.example.com/mcp", "roles": []},
]


@pytest.fixture
def granted(client: TestClient) -> Iterator[TestClient]:
    """The app with three connected (fake) MCP servers and their grants stored."""
    app: Any = client.app
    source = app.state.instance_settings

    async def store() -> None:
        await source.set("mcp.servers", SERVERS, actor="test")

    client.portal.call(store)  # type: ignore[attr-defined]
    manager = MCPManager(client_factory=FakeClient)
    for entry in SERVERS:
        manager.add_server(
            MCPServerConfig(name=entry["name"], transport=entry["transport"], url=entry["url"])
        )

    async def connect() -> None:
        await manager.connect_all()
        await manager.list_all_tools()

    client.portal.call(connect)  # type: ignore[attr-defined]
    app.state.mcp_manager = manager
    app.state.conversation_manager._mcp_manager = manager
    yield client


def _tool_panel(browser: TestClient, client: TestClient, guid: str) -> set[str]:
    cid = _call(client, conversations.create_conversation, "Tools", "model-a", guid)
    resp = browser.get(f"/chat/{cid}/api/tools", headers={"Accept": "application/json"})
    assert resp.status_code == 200, resp.text
    return {s["name"] for s in resp.json()["mcp_servers"]}


class TestGrantedServers:
    def test_a_user_sees_only_the_servers_of_their_role(self, granted: TestClient) -> None:
        ada, guid = _signed_in(granted, "ada@example.com", USER)
        assert _tool_panel(ada, granted, guid) == {"search"}

    def test_a_power_user_sees_the_servers_of_their_role(self, granted: TestClient) -> None:
        grace, guid = _signed_in(granted, "grace@example.com", POWER_USER)
        assert _tool_panel(grace, granted, guid) == {"finance"}

    def test_every_role_counts(self, granted: TestClient) -> None:
        both, guid = _signed_in(granted, "both@example.com", USER, POWER_USER)
        assert _tool_panel(both, granted, guid) == {"search", "finance"}

    def test_a_role_without_a_grant_sees_none(self, granted: TestClient) -> None:
        root, guid = _signed_in(granted, "root@example.com", ADMINISTRATOR)
        assert _tool_panel(root, granted, guid) == set()

    def test_the_main_menu_lists_only_granted_servers(self, granted: TestClient) -> None:
        ada, _ = _signed_in(granted, "ada@example.com", USER)
        page = ada.get("/menu")
        assert page.status_code == 200
        assert "search" in page.text
        assert "finance" not in page.text and "spare" not in page.text

    def test_the_debate_capabilities_list_only_granted_servers(self, granted: TestClient) -> None:
        ada, _ = _signed_in(granted, "ada@example.com", USER)
        resp = ada.get("/debate/api/capabilities", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        groups = {g["group"] for g in resp.json()["tools"] if g["group"].startswith("MCP: ")}
        assert groups == {"MCP: search"}

    def test_the_action_tool_list_has_only_granted_servers(self, granted: TestClient) -> None:
        from spark.database import autonomous_actions

        grace, guid = _signed_in(granted, "grace@example.com", POWER_USER)
        action = _call(granted, autonomous_actions.create_action, "nightly", "p", "m", guid)
        resp = grace.get(f"/actions/api/{action}/tools", headers={"Accept": "application/json"})
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()["tools"]}
        assert "finance_lookup" in names
        assert "search_lookup" not in names and "spare_lookup" not in names

    def test_a_revoked_grant_takes_effect_on_the_next_request(self, granted: TestClient) -> None:
        ada, guid = _signed_in(granted, "ada@example.com", USER)
        assert _tool_panel(ada, granted, guid) == {"search"}
        source: Any = granted.app.state.instance_settings  # type: ignore[attr-defined]

        async def revoke() -> None:
            await source.set(
                "mcp.servers", [{**SERVERS[0], "roles": [AUDITOR]}, *SERVERS[1:]], actor="test"
            )

        granted.portal.call(revoke)  # type: ignore[attr-defined]
        assert _tool_panel(ada, granted, guid) == set()

    def test_the_settings_api_stays_an_administrators(self, granted: TestClient) -> None:
        ada, _ = _signed_in(granted, "ada@example.com", USER)
        assert ada.get("/settings/mcp/api/list").status_code == 403
        root, _ = _signed_in(granted, "root@example.com", ADMINISTRATOR)
        listed = root.get("/settings/mcp/api/list")
        assert listed.status_code == 200
        assert {s["name"] for s in listed.json()} == {"search", "finance", "spare"}
