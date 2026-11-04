"""MCP servers in server mode: transports, names, grants and credentials (A12, A9).

A server allows HTTP and SSE MCP servers only; a stdio server runs a command on
the host and is refused. Each server is granted to roles; the grant shows as the
``spark.mcp.<server>`` permission. Credentials never stay in the server entry:
on a server they live in konfig's secrets store.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from spark.mcp_integration import servers
from spark.mcp_integration.servers import ServerEntryError


def _http(name: str = "search", **extra: Any) -> dict[str, Any]:
    return {"name": name, "transport": "http", "url": "https://mcp.example.com/mcp", **extra}


# -- Transports ----------------------------------------------------------------


class TestTransports:
    def test_server_mode_refuses_stdio_with_a_clear_message(self) -> None:
        entry = {"name": "local", "transport": "stdio", "command": "npx"}
        with pytest.raises(ServerEntryError) as caught:
            servers.validate_entry(entry, server=True)
        assert "stdio" in str(caught.value)
        assert "HTTP or SSE" in str(caught.value)

    @pytest.mark.parametrize("transport", ["http", "sse"])
    def test_server_mode_allows_http_and_sse(self, transport: str) -> None:
        entry = servers.validate_entry(_http(transport=transport), server=True)
        assert entry["transport"] == transport

    def test_personal_mode_still_allows_stdio(self) -> None:
        entry = {"name": "Local Tools", "transport": "stdio", "command": "npx"}
        assert servers.validate_entry(entry, server=False)["transport"] == "stdio"

    def test_unknown_transport_is_refused_in_both_modes(self) -> None:
        for server in (True, False):
            with pytest.raises(ServerEntryError):
                servers.validate_entry(_http(transport="websocket"), server=server)

    def test_server_mode_needs_an_http_url(self) -> None:
        with pytest.raises(ServerEntryError, match="url"):
            servers.validate_entry(_http(url=""), server=True)
        with pytest.raises(ServerEntryError, match="url"):
            servers.validate_entry(_http(url="file:///etc/passwd"), server=True)

    def test_stored_stdio_entries_are_ignored_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored = [_http(), {"name": "local", "transport": "stdio", "command": "npx"}]
        with caplog.at_level(logging.WARNING, logger="spark.mcp_integration.servers"):
            kept = servers.stored_entries(stored, server=True)
        assert [e["name"] for e in kept] == ["search"]
        assert "local" in caplog.text and "stdio" in caplog.text

    def test_stored_entries_keep_stdio_in_personal_mode(self) -> None:
        stored = [{"name": "local", "transport": "stdio", "command": "npx"}]
        assert servers.stored_entries(stored, server=False) == stored


# -- Names and permissions -----------------------------------------------------


class TestNames:
    def test_permission_name(self) -> None:
        assert servers.permission_name("search") == "spark.mcp.search"

    @pytest.mark.parametrize("name", ["Search", "has space", "dot.ted", "", "-lead", "x" * 70])
    def test_server_mode_names_are_lower_case_words(self, name: str) -> None:
        with pytest.raises(ServerEntryError, match="name"):
            servers.validate_entry(_http(name), server=True)

    @pytest.mark.parametrize("name", ["search", "web-tools", "kb_2"])
    def test_server_mode_accepts_simple_names(self, name: str) -> None:
        assert servers.validate_entry(_http(name), server=True)["name"] == name


# -- Grants --------------------------------------------------------------------


class TestGrants:
    def test_roles_are_kept(self) -> None:
        entry = servers.validate_entry(_http(roles=["user", "power_user"]), server=True)
        assert entry["roles"] == ["user", "power_user"]

    def test_a_server_without_roles_is_granted_to_no_one(self) -> None:
        entry = servers.validate_entry(_http(), server=True)
        assert entry["roles"] == []
        assert servers.grant_permissions([entry], frozenset({"administrator"})) == frozenset()

    def test_unknown_role_is_refused(self) -> None:
        with pytest.raises(ServerEntryError, match="role"):
            servers.validate_entry(_http(roles=["superuser"]), server=True)

    def test_roles_must_be_a_list_of_names(self) -> None:
        with pytest.raises(ServerEntryError, match="role"):
            servers.validate_entry(_http(roles="user"), server=True)

    def test_grant_permissions_follow_the_users_roles(self) -> None:
        entries = [
            _http("search", roles=["user"]),
            _http("finance", roles=["power_user"]),
            _http("off", roles=["user"], enabled=False),
        ]
        assert servers.grant_permissions(entries, frozenset({"user"})) == {"spark.mcp.search"}
        assert servers.grant_permissions(entries, frozenset({"user", "power_user"})) == {
            "spark.mcp.search",
            "spark.mcp.finance",
        }
        assert servers.grant_permissions(entries, frozenset({"auditor"})) == frozenset()

    def test_grant_permissions_ignore_stdio_entries(self) -> None:
        entries = [{"name": "local", "transport": "stdio", "roles": ["user"]}]
        assert servers.grant_permissions(entries, frozenset({"user"})) == frozenset()

    def test_is_granted(self) -> None:
        assert servers.is_granted("search", frozenset({"spark.mcp.search"}))
        assert not servers.is_granted("search", frozenset({"spark.mcp.searcher"}))
        assert not servers.is_granted("search", frozenset())
        assert not servers.is_granted("search", None)


# -- Credentials ---------------------------------------------------------------


class TestCredentials:
    def test_server_mode_entry_may_not_carry_a_credential(self) -> None:
        for field, value in (
            ("auth_token", "tok"),
            ("basic_password", "pw"),
            ("custom_headers", {"X-Key": "abc"}),
        ):
            with pytest.raises(ServerEntryError, match="secrets store"):
                servers.validate_entry(_http(**{field: value}), server=True)

    def test_split_secrets_takes_credentials_out_of_the_entry(self) -> None:
        plain, secret = servers.split_secrets(
            _http(auth_type="bearer", auth_token="tok", custom_headers={"X-Key": "abc"})
        )
        assert "auth_token" not in plain and "custom_headers" not in plain
        assert secret == {"auth_token": "tok", "custom_headers": '{"X-Key": "abc"}'}

    def test_split_secrets_ignores_empty_values(self) -> None:
        plain, secret = servers.split_secrets(_http(auth_token="", custom_headers={}))
        assert secret == {}
        assert "auth_token" not in plain

    def test_secret_names_are_per_server_and_field(self) -> None:
        assert servers.secret_name("search", "auth_token") == "mcp_server_search_auth_token"
        with pytest.raises(ValueError):
            servers.secret_name("search", "url")

    def test_to_config_reads_credentials_from_the_store(self) -> None:
        store = {
            "mcp_server_search_auth_token": "tok",
            "mcp_server_search_custom_headers": '{"X-Key": "abc"}',
        }
        config = servers.to_config(
            servers.validate_entry(_http(auth_type="bearer", timeout=12), server=True),
            store.get,
        )
        assert config.name == "search"
        assert config.transport == "http"
        assert config.auth_token == "tok"
        assert config.custom_headers == {"X-Key": "abc"}
        assert config.timeout == pytest.approx(12.0)
        assert config.command is None

    def test_to_config_without_a_store_uses_the_entry(self) -> None:
        config = servers.to_config(
            {"name": "Local", "transport": "stdio", "command": "npx", "args": ["-y", "x"]}, None
        )
        assert config.command == "npx" and config.args == ["-y", "x"]

    def test_unreadable_custom_headers_secret_is_ignored(self) -> None:
        config = servers.to_config(
            servers.validate_entry(_http(), server=True),
            {"mcp_server_search_custom_headers": "not json"}.get,
        )
        assert config.custom_headers == {}

    def test_public_entry_never_shows_a_credential(self) -> None:
        public = servers.public_entry(
            _http(auth_type="bearer", auth_token="tok", basic_password="pw", env={"K": "v"}),
            {"mcp_server_search_auth_token": "tok"}.get,
        )
        assert "auth_token" not in public and "basic_password" not in public
        assert "env" not in public
        assert public["secrets_set"] == {
            "auth_token": True,
            "basic_password": False,
            "custom_headers": False,
        }
