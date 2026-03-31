"""Tests for chat, conversation, and streaming endpoints."""

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
def mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    return ctx


@pytest.fixture()
def client(mock_ctx: MagicMock) -> TestClient:
    app = create_app(mock_ctx, first_run=False)
    return TestClient(app)


def _auth(client: TestClient) -> dict:
    """Authenticate and return cookies dict."""
    code = client.app.state.auth.generate_code()
    resp = client.post("/api/auth", data={"code": code}, follow_redirects=False)
    return {"spark_session": resp.cookies.get("spark_session", "")}


class TestConversationsPage:
    def test_page_loads(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/conversations", cookies=cookies)
        assert resp.status_code == 200
        assert "Conversations" in resp.text

    def test_api_list_empty(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/conversations/api/list", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_api_models_empty(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/conversations/api/models", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["mandatory"] is False

    def test_api_create_no_manager(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.post(
            "/conversations/api/create",
            cookies=cookies,
            json={"name": "Test", "model_id": "m1"},
        )
        assert resp.status_code == 503

    def test_api_delete_no_manager(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.request("DELETE", "/conversations/api/1", cookies=cookies)
        assert resp.status_code == 503


class TestChatPage:
    def test_page_loads_without_manager(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/chat/1", cookies=cookies)
        assert resp.status_code == 200
        # Should show the chat template even without a real conversation
        assert "chat-messages" in resp.text

    def test_api_history_empty(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get("/chat/1/api/history", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_api_send_no_manager(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.post(
            "/chat/1/api/send",
            cookies=cookies,
            json={"message": "Hello"},
        )
        assert resp.status_code == 503

    def test_api_send_empty_message(self, client: TestClient) -> None:
        cookies = _auth(client)
        # Set up a mock conversation manager
        mock_mgr = MagicMock()
        client.app.state.conversation_manager = mock_mgr
        resp = client.post(
            "/chat/1/api/send",
            cookies=cookies,
            json={"message": ""},
        )
        assert resp.status_code == 400

    def test_permission_respond(self, client: TestClient) -> None:
        cookies = _auth(client)
        # Set up a pending permission event to signal
        import threading

        event = threading.Event()
        client.app.state.permission_events = {"t1": event}
        client.app.state.permission_responses = {"t1": None}

        resp = client.post(
            "/chat/permission/respond",
            cookies=cookies,
            json={"request_id": "t1", "decision": "allowed"},
        )
        assert resp.status_code == 200
        assert event.is_set()
        assert client.app.state.permission_responses["t1"] == "allowed"


class TestChatWithManager:
    def test_api_history(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.get_messages.return_value = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        client.app.state.conversation_manager = mock_mgr

        resp = client.get("/chat/1/api/history", cookies=cookies)
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 2

    def test_api_send(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.send_message.return_value = {
            "content": "Response text",
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "tool_calls": [],
            "iterations": 1,
        }
        client.app.state.conversation_manager = mock_mgr

        resp = client.post(
            "/chat/1/api/send",
            cookies=cookies,
            json={"message": "Hello"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Response text"
        mock_mgr.send_message.assert_called_once()

    def test_chat_page_with_conversation(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.get_conversation.return_value = {
            "id": 1,
            "name": "Test Chat",
            "model_id": "claude-3",
        }
        client.app.state.conversation_manager = mock_mgr

        resp = client.get("/chat/1", cookies=cookies)
        assert resp.status_code == 200
        assert "Test Chat" in resp.text


class TestConversationsWithManager:
    def test_api_list(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.get_conversations.return_value = [
            {"id": 1, "name": "Chat 1", "model_id": "m1", "total_tokens": 100},
        ]
        client.app.state.conversation_manager = mock_mgr

        resp = client.get("/conversations/api/list", cookies=cookies)
        assert resp.status_code == 200
        convs = resp.json()
        assert len(convs) == 1
        assert convs[0]["name"] == "Chat 1"

    def test_api_create(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        mock_mgr.create_conversation.return_value = 42
        client.app.state.conversation_manager = mock_mgr

        resp = client.post(
            "/conversations/api/create",
            cookies=cookies,
            json={"name": "New Chat", "model_id": "claude-3"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 42

    def test_api_delete(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_mgr = MagicMock()
        client.app.state.conversation_manager = mock_mgr

        resp = client.request("DELETE", "/conversations/api/5", cookies=cookies)
        assert resp.status_code == 200
        mock_mgr.delete_conversation.assert_called_once()

    def test_api_models(self, client: TestClient) -> None:
        cookies = _auth(client)
        mock_llm = MagicMock()
        mock_llm.list_all_models.return_value = [
            {"id": "m1", "name": "Model 1", "provider": "Test"},
        ]
        client.app.state.llm_manager = mock_llm

        resp = client.get("/conversations/api/models", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 1
        assert data["mandatory"] is False


class TestStreaming:
    def test_stream_no_manager(self, client: TestClient) -> None:
        cookies = _auth(client)
        resp = client.get(
            "/stream/chat?message=hello&conversation_id=1",
            cookies=cookies,
        )
        # Should return an error event via SSE
        assert resp.status_code == 200
        assert "Not initialised" in resp.text
