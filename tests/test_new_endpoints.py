"""Tests for the new chat settings, memories, and actions endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.web.server import create_app


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    values = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
        "interface.ssl.enabled": False,
        "providers.anthropic.enabled": False,
        "providers.aws_bedrock.enabled": False,
        "providers.ollama.enabled": False,
        "providers.google_gemini.enabled": False,
        "providers.xai.enabled": False,
    }
    val = values.get(key, default)
    if cast is not None and val is not None:
        val = cast(val)
    return val


@pytest.fixture()
def client() -> TestClient:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    return TestClient(app)


def _auth(client: TestClient) -> dict:
    code = client.app.state.auth.generate_code()
    resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
    return {"spark_session": resp.cookies.get("spark_session", "")}


class TestExpandedChatSettings:
    def test_info_includes_rag_fields(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.get_conversation.return_value = {
            "id": 1, "name": "Test", "model_id": "m1",
            "tokens_sent": 100, "tokens_received": 50, "total_tokens": 150,
            "instructions": None, "compaction_threshold": 0.7,
            "compaction_summary_ratio": 0.3, "memory_enabled": 1,
            "rag_enabled": 1, "rag_top_k": 5, "rag_threshold": 0.4,
            "rag_tool_enabled": 0, "max_history_messages": None,
            "include_tool_results": 1, "created_at": "2026-01-01",
        }
        client.app.state.conversation_manager = mock_mgr
        resp = client.get("/chat/1/api/info", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "rag_enabled" in data
        assert "rag_top_k" in data
        assert "max_history_messages" in data

    def test_settings_update_rag(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr._db = MagicMock()
        client.app.state.conversation_manager = mock_mgr

        import spark.database.conversations as conv_mod
        original = conv_mod.update_conversation
        calls = []
        def mock_update(db, cid, uid, **kwargs):
            calls.append(kwargs)
        conv_mod.update_conversation = mock_update

        try:
            resp = client.post("/chat/1/api/settings", cookies=cookies,
                json={"rag_enabled": False, "rag_top_k": 3, "max_history_messages": 20})
            assert resp.status_code == 200
            assert calls[0]["rag_enabled"] == 0
            assert calls[0]["rag_top_k"] == 3
            assert calls[0]["max_history_messages"] == 20
        finally:
            conv_mod.update_conversation = original


class TestConversationTools:
    def test_get_tools(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/chat/1/api/tools", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "embedded" in data
        assert "mcp_servers" in data


class TestConversationLinks:
    def test_get_links_no_manager(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/chat/1/api/links", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "linked" in data
        assert "available" in data


class TestConversationExport:
    def test_export_markdown(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.get_conversation.return_value = {"id": 1, "name": "Test"}
        mock_mgr.get_messages.return_value = [
            {"role": "user", "content": "Hello", "timestamp": "2026-01-01"},
            {"role": "assistant", "content": "Hi!", "timestamp": "2026-01-01"},
        ]
        client.app.state.conversation_manager = mock_mgr
        resp = client.get("/chat/1/api/export?format=markdown", cookies=cookies)
        assert resp.status_code == 200
        assert "# Test" in resp.text
        assert "Hello" in resp.text

    def test_export_json(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.get_conversation.return_value = {"id": 1, "name": "Test"}
        mock_mgr.get_messages.return_value = []
        client.app.state.conversation_manager = mock_mgr
        resp = client.get("/chat/1/api/export?format=json", cookies=cookies)
        assert resp.status_code == 200


class TestMemoriesPage:
    def test_page_loads(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/memories", cookies=cookies)
        assert resp.status_code == 200
        assert "Memories" in resp.text

    def test_api_list_no_db(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/memories/api/list", cookies=cookies)
        assert resp.status_code == 200

    def test_api_stats_no_db(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/memories/api/stats", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestActionsPage:
    def test_page_redirects_when_disabled(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/actions", cookies=cookies, follow_redirects=False)
        assert resp.status_code == 303  # Redirects to / when disabled

    def test_api_returns_403_when_disabled(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/actions/api/list", cookies=cookies)
        assert resp.status_code == 403

    def test_page_loads_when_enabled(self, client: TestClient) -> None:
        cookies = _auth(client)
        # Enable actions in settings
        client.app.state.ctx.settings.get = lambda key, default=None, **kw: (
            True if key == "autonomous_actions.enabled" else
            _mock_settings_get(key, default, **kw)
        )
        resp = client.get("/actions", cookies=cookies)
        assert resp.status_code == 200
        assert "Autonomous Actions" in resp.text
