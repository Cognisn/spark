"""The ``mcp.servers`` instance setting in server mode (A12): configuration refuses stdio.

Every write path validates through :func:`spark.instance_settings.registry.coerce`,
so a stdio server is refused however it is configured. A stored value is read
with ``stored=True``: a stdio entry already there is ignored with a warning and
the other servers are kept.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from spark import mode
from spark.instance_settings import registry
from spark.instance_settings.registry import InvalidSettingError
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

HTTP = {"name": "search", "transport": "http", "url": "https://mcp.example.com/mcp"}
STDIO = {"name": "local", "transport": "stdio", "command": "npx"}


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


class TestServerMode:
    def test_stdio_is_refused_with_a_clear_message(self, server_mode: None) -> None:
        with pytest.raises(InvalidSettingError) as caught:
            registry.coerce("mcp.servers", [HTTP, STDIO])
        assert caught.value.key == "mcp.servers"
        assert "stdio" in str(caught.value) and "HTTP or SSE" in str(caught.value)

    def test_http_entry_is_stored_with_its_roles(self, server_mode: None) -> None:
        stored = registry.coerce("mcp.servers", [{**HTTP, "roles": ["user"]}])
        assert stored == [{**HTTP, "roles": ["user"]}]

    def test_a_credential_in_the_setting_is_refused(self, server_mode: None) -> None:
        with pytest.raises(InvalidSettingError, match="secrets store"):
            registry.coerce("mcp.servers", [{**HTTP, "auth_token": "tok"}])

    def test_error_never_echoes_a_credential(self, server_mode: None) -> None:
        with pytest.raises(InvalidSettingError) as caught:
            registry.coerce("mcp.servers", [{**HTTP, "auth_token": "s3cr3t-value"}])
        assert "s3cr3t-value" not in str(caught.value)

    def test_repeated_names_are_still_refused(self, server_mode: None) -> None:
        with pytest.raises(InvalidSettingError, match="repeated"):
            registry.coerce("mcp.servers", [HTTP, HTTP])

    def test_stored_stdio_is_ignored_with_a_warning(
        self, server_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            stored = registry.coerce("mcp.servers", [HTTP, STDIO], stored=True)
        assert [e["name"] for e in stored] == ["search"]
        assert "local" in caplog.text

    def test_stored_credential_is_dropped_with_a_warning(
        self, server_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            stored = registry.coerce(
                "mcp.servers", [{**HTTP, "auth_token": "s3cr3t-value"}], stored=True
            )
        assert stored == [{**HTTP, "roles": []}]
        assert "secrets store" in caplog.text and "s3cr3t-value" not in caplog.text

    def test_stored_invalid_entry_is_ignored(
        self, server_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            stored = registry.coerce(
                "mcp.servers", [HTTP, {**HTTP, "name": "Bad Name"}], stored=True
            )
        assert [e["name"] for e in stored] == ["search"]

    def test_stored_flag_does_not_change_other_keys(self, server_mode: None) -> None:
        assert registry.coerce("conversation.max_tool_iterations", "7", stored=True) == 7


class TestPersonalMode:
    def test_stdio_is_still_accepted(self) -> None:
        assert registry.coerce("mcp.servers", [HTTP, STDIO]) == [HTTP, STDIO]
        assert registry.coerce("mcp.servers", [HTTP, STDIO], stored=True) == [HTTP, STDIO]
