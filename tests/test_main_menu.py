"""Tests for main menu endpoint helpers."""

from unittest.mock import MagicMock

from spark.web.endpoints.main_menu import (
    _get_embedded_tool_summary,
    _get_mcp_server_list,
    _get_mcp_stats,
    _get_provider_summary,
)


class TestGetProviderSummary:
    def test_returns_all_providers(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=False)
        providers = _get_provider_summary(ctx, None)
        assert len(providers) == 5

    def test_provider_keys(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=False)
        providers = _get_provider_summary(ctx, None)
        keys = {p["key"] for p in providers}
        assert keys == {"anthropic", "aws_bedrock", "ollama", "google_gemini", "xai"}

    def test_provider_structure(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=False)
        providers = _get_provider_summary(ctx, None)
        for p in providers:
            assert "key" in p
            assert "name" in p
            assert "icon" in p
            assert "enabled" in p
            assert "model_count" in p

    def test_enabled_flag(self) -> None:
        ctx = MagicMock()

        def mock_get(key: str, default: object = None) -> object:
            if key == "providers.anthropic.enabled":
                return True
            return False

        ctx.settings.get = mock_get
        providers = _get_provider_summary(ctx, None)
        anthropic = next(p for p in providers if p["key"] == "anthropic")
        ollama = next(p for p in providers if p["key"] == "ollama")
        assert anthropic["enabled"] is True
        assert ollama["enabled"] is False

    def test_model_count_from_llm_manager(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=True)

        llm_mgr = MagicMock()
        llm_mgr.list_all_models.return_value = [
            {"id": "m1", "provider": "Anthropic"},
            {"id": "m2", "provider": "Anthropic"},
            {"id": "m3", "provider": "Google Gemini"},
        ]

        providers = _get_provider_summary(ctx, llm_mgr)
        anthropic = next(p for p in providers if p["key"] == "anthropic")
        gemini = next(p for p in providers if p["key"] == "google_gemini")
        ollama = next(p for p in providers if p["key"] == "ollama")
        assert anthropic["model_count"] == 2
        assert gemini["model_count"] == 1
        assert ollama["model_count"] == 0

    def test_model_count_no_manager(self) -> None:
        ctx = MagicMock()
        ctx.settings.get = MagicMock(return_value=False)
        providers = _get_provider_summary(ctx, None)
        for p in providers:
            assert p["model_count"] == 0


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

    def _client(self, tmp_path):
        import pytest  # noqa: F401 - fixture-free client construction
        from fastapi.testclient import TestClient

        from spark.database import Database
        from spark.database.backends import SQLiteBackend
        from spark.database.connection import DatabaseConnection
        from spark.web.server import create_app

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

        backend = SQLiteBackend(tmp_path / "test.db")
        conn = DatabaseConnection(backend)
        Database(conn)

        mgr = MagicMock()
        mgr._db = conn

        def _get_convs(user_guid):
            from spark.database import conversations as convdb

            return convdb.get_active_conversations(conn, user_guid)

        mgr.get_conversations.side_effect = _get_convs
        app.state.conversation_manager = mgr
        app.state.user_guid = "u1"
        client = TestClient(app)
        code = app.state.auth.generate_code()
        resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
        client.cookies.set("spark_session", resp.cookies.get("spark_session", ""))
        return client, conn

    def test_home_lists_route_by_conversation_type(self, tmp_path) -> None:
        client, conn = self._client(tmp_path)
        ph = conn.placeholder
        ids = {}
        for name, ctype, fav in (
            ("Std", "standard", 0),
            ("Deb", "debate", 1),
            ("Pan", "panel", 0),
        ):
            cur = conn.execute(
                f"INSERT INTO conversations (name, model_id, user_guid, "
                f"conversation_type, is_favourite) VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                (name, "m", "u1", ctype, fav),
            )
            ids[ctype] = cur.lastrowid
        conn.commit()

        html = client.get("/").text
        assert f'href="/chat/{ids["standard"]}"' in html
        assert f'href="/debate/{ids["debate"]}"' in html
        assert f'href="/panel/{ids["panel"]}"' in html
        assert f'href="/chat/{ids["debate"]}"' not in html
        assert f'href="/chat/{ids["panel"]}"' not in html
