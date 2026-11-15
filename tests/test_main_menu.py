import pytest

"""Tests for main menu endpoint helpers."""

from unittest.mock import MagicMock

from spark.web.endpoints.main_menu import (
    _get_embedded_tool_summary,
    _get_mcp_server_list,
    _get_mcp_stats,
    _get_provider_summary,
)
from tests.web_helpers import sign_in


def _request(connections: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(connections=connections)))


class TestGetProviderSummary:
    """The dashboard's model connections: one entry per connection the user may use."""

    @pytest.fixture
    def runtime(self) -> object:
        from tests.model_helpers import stub_connection, stub_runtime

        rt = stub_runtime(
            connections=[
                stub_connection("work"),
                stub_connection("home"),
                stub_connection("off", enabled=False),
            ]
        )
        yield rt
        rt.close()

    def test_one_entry_per_connection(self, runtime: object) -> None:
        providers = _get_provider_summary(_request(runtime))  # type: ignore[arg-type]
        assert [p["key"] for p in providers] == ["home", "off", "work"]
        for p in providers:
            assert {"key", "name", "icon", "enabled", "model_count"} <= set(p)

    def test_enabled_flag_and_model_counts(self, runtime: object) -> None:
        providers = {p["key"]: p for p in _get_provider_summary(_request(runtime))}  # type: ignore[arg-type]
        assert providers["work"]["enabled"] is True and providers["work"]["model_count"] == 1
        assert providers["off"]["enabled"] is False and providers["off"]["model_count"] == 0
        assert providers["work"]["name"] == "Stub work"

    def test_no_runtime_no_connections(self) -> None:
        assert _get_provider_summary(_request(None)) == []  # type: ignore[arg-type]


class TestGetEmbeddedToolSummary:
    def test_returns_all_categories(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=None)
        tools = _get_embedded_tool_summary(ctx)
        assert len(tools) == 4

    def test_tool_keys(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=None)
        tools = _get_embedded_tool_summary(ctx)
        keys = {t["key"] for t in tools}
        assert keys == {"filesystem", "documents", "archives", "web"}

    def test_tool_structure(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=None)
        tools = _get_embedded_tool_summary(ctx)
        for t in tools:
            assert "key" in t
            assert "name" in t
            assert "icon" in t
            assert "enabled" in t
            assert "tool_count" in t

    def test_enabled_with_paths_has_tool_count(self) -> None:
        ctx = MagicMock()

        def mock_get(key: str, default: object = None) -> object:
            if "enabled" in str(key):
                return True
            if "allowed_paths" in str(key):
                return ["/tmp"]
            return default

        ctx.settings.get = mock_get
        tools = _get_embedded_tool_summary(ctx)
        for t in tools:
            if t["enabled"]:
                assert t["tool_count"] > 0

    def test_enabled_without_paths_excludes_path_tools(self) -> None:
        ctx = MagicMock()

        def mock_get(key: str, default: object = None) -> object:
            if "enabled" in str(key):
                return True
            if "allowed_paths" in str(key):
                return []
            return default

        ctx.settings.get = mock_get
        tools = _get_embedded_tool_summary(ctx)
        # Web should still have tools (no paths needed)
        web = next(t for t in tools if t["key"] == "web")
        assert web["tool_count"] > 0
        # Filesystem should have 0 tools without paths
        fs = next(t for t in tools if t["key"] == "filesystem")
        assert fs["tool_count"] == 0

    def test_disabled_has_zero_count(self) -> None:
        ctx = MagicMock()

        def mock_get(key: str, default: object = None) -> object:
            if "enabled" in str(key):
                return False
            return default

        ctx.settings.get = mock_get
        tools = _get_embedded_tool_summary(ctx)
        for t in tools:
            assert t["tool_count"] == 0


class TestGetMCPStats:
    def test_with_embedded(self) -> None:
        ctx = MagicMock()
        embedded = [
            {"key": "filesystem", "enabled": True, "tool_count": 6},
            {"key": "web", "enabled": True, "tool_count": 2},
        ]
        stats = _get_mcp_stats(ctx, embedded)
        assert stats["servers_configured"] >= 1
        assert stats["total_tools"] == 8
        assert stats["servers_failed"] == 0

    def test_all_disabled(self) -> None:
        ctx = MagicMock()
        embedded = [
            {"key": "filesystem", "enabled": False, "tool_count": 0},
        ]
        stats = _get_mcp_stats(ctx, embedded)
        assert stats["servers_configured"] == 0
        assert stats["total_tools"] == 0


class TestGetMCPServerList:
    def test_embedded_shown(self) -> None:
        ctx = MagicMock()
        embedded = [
            {"key": "filesystem", "enabled": True, "tool_count": 6},
            {"key": "web", "enabled": True, "tool_count": 2},
        ]
        servers = _get_mcp_server_list(ctx, embedded)
        assert len(servers) == 1
        assert servers[0]["name"] == "Embedded Tools"
        assert servers[0]["type"] == "embedded"
        assert servers[0]["tool_count"] == 8
        assert servers[0]["connected"] is True

    def test_no_embedded_when_all_disabled(self) -> None:
        ctx = MagicMock()
        embedded = [
            {"key": "filesystem", "enabled": False, "tool_count": 0},
            {"key": "web", "enabled": False, "tool_count": 0},
        ]
        servers = _get_mcp_server_list(ctx, embedded)
        assert len(servers) == 0


class TestMenuConversationLinks:
    """The Home lists must route each conversation to its type's view."""

    @pytest.fixture()
    def menu(self, sqlite_settings):
        from spark.web.server import create_app
        from tests.db_helpers import app_client

        def _settings_get(key, default=None, *, cast=None):
            values = {
                "interface.session_timeout_minutes": 60,
                "interface.host": "127.0.0.1",
                "interface.ssl.enabled": False,
            }
            val = values.get(key, default)
            if cast is not None and val is not None:
                val = cast(val)
            return val

        ctx = MagicMock()
        ctx.settings.get = _settings_get
        app = create_app(ctx, first_run=False)
        app.state.user_guid = "u1"
        with app_client(app, sqlite_settings) as client:
            sync_db = app.state.sync_db
            mgr = MagicMock()
            mgr._db = sync_db

            def _get_convs(user_guid):
                from spark.database import conversations as convdb

                return sync_db.call(convdb.get_active_conversations, user_guid)

            mgr.get_conversations.side_effect = _get_convs
            app.state.conversation_manager = mgr
            sign_in(client)
            yield client, sync_db

    def test_home_lists_route_by_conversation_type(self, menu) -> None:
        client, conn = menu
        ids = {}
        for name, ctype, fav in (
            ("Std", "standard", 0),
            ("Deb", "debate", 1),
            ("Pan", "panel", 0),
        ):
            ids[ctype] = conn.fetch_value(
                "INSERT INTO conversations (name, model_id, user_guid, "
                "conversation_type, is_favourite) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (name, "m", "u1", ctype, fav),
            )

        html = client.get("/").text
        assert f'href="/chat/{ids["standard"]}"' in html
        assert f'href="/debate/{ids["debate"]}"' in html
        assert f'href="/panel/{ids["panel"]}"' in html
        assert f'href="/chat/{ids["debate"]}"' not in html
        assert f'href="/chat/{ids["panel"]}"' not in html
