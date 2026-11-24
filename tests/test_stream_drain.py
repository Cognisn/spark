"""The chat stream forwards every event queued when the turn ends.

The compactor runs after the reply and reports ``compaction_start`` and
``compaction_complete`` through the turn's status callback; those events can
be queued after the polling loop's last look, so the drain after the turn's
future completes must map every kind exactly as the polling loop does.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.web.endpoints import streaming
from spark.web.server import create_app
from tests.web_helpers import every_conversation_owned, sign_in

INSPECTION = {
    "tool_name": "web_fetch",
    "source": "web",
    "action": "warn",
    "categories": ["injection"],
    "severity": "high",
    "explanation": "matched prompt injection patterns",
}


def _settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    values = {"interface.session_timeout_minutes": 60, "interface.host": "127.0.0.1"}
    val = values.get(key, default)
    return cast(val) if cast is not None and val is not None else val


class _DoneFirstLoop:
    """An event loop whose executor has already run the turn when asked.

    The turn runs to completion inside ``run_in_executor`` and its future is
    done before the stream's polling loop looks at it, so every event the turn
    queued is still queued when the drain starts. This makes the loss
    deterministic instead of a matter of timing.
    """

    def __init__(self, real: asyncio.AbstractEventLoop) -> None:
        self._real = real

    def run_in_executor(self, executor: object, func: Callable[[], Any]) -> asyncio.Future:
        future: asyncio.Future = self._real.create_future()
        try:
            future.set_result(func())
        except BaseException as exc:  # the turn's own failure, as the executor would
            future.set_exception(exc)
        return future


@pytest.fixture()
def done_first(monkeypatch: pytest.MonkeyPatch) -> None:
    real_get_event_loop = asyncio.get_event_loop
    monkeypatch.setattr(
        streaming.asyncio, "get_event_loop", lambda: _DoneFirstLoop(real_get_event_loop())
    )


def _client(monkeypatch: pytest.MonkeyPatch, send: Callable[..., Any]) -> tuple[TestClient, dict]:
    ctx = MagicMock()
    ctx.settings.get = _settings_get
    app = create_app(ctx, first_run=False)
    client = TestClient(app)
    cookies = sign_in(client)
    mgr = MagicMock()
    mgr.send_message.side_effect = send
    app.state.conversation_manager = mgr

    # No database here: the id is the local user's.
    every_conversation_owned(monkeypatch)
    return client, cookies


def _events(client: TestClient, cookies: dict) -> list[tuple[str, Any]]:
    body = client.get("/stream/chat?message=hello&conversation_id=1", cookies=cookies).text
    found: list[tuple[str, Any]] = []
    for block in body.split("\r\n\r\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            found.append((lines["event"], json.loads(lines["data"]) if "data" in lines else None))
    return found


def _result(**extra: Any) -> dict[str, Any]:
    return {
        "content": "Done",
        "status": "completed",
        "usage": {},
        "tool_calls": [],
        "iterations": 1,
        **extra,
    }


def _compacting_turn(cid: int, message: str, user_guid: str, **kwargs: Any) -> dict[str, Any]:
    cb = kwargs["status_callback"]
    cb("compaction_start", {"messages": 12, "tokens": 9000})
    cb("compaction_complete", {"messages_compacted": 10, "tokens_saved": 7000})
    return _result()


class TestCompactionEventsAfterTheTurn:
    def test_both_compaction_events_reach_the_client(
        self, monkeypatch: pytest.MonkeyPatch, done_first: None
    ) -> None:
        client, cookies = _client(monkeypatch, _compacting_turn)
        events = _events(client, cookies)
        names = [name for name, _ in events]
        assert names == [
            "status",
            "compaction_status",
            "compaction_status",
            "response",
            "complete",
        ]
        first, second = (data for name, data in events if name == "compaction_status")
        assert first == {"status": "start", "messages": 12, "tokens": 9000}
        assert second == {"status": "complete", "messages_compacted": 10, "tokens_saved": 7000}

    def test_without_the_deterministic_loop_the_events_still_arrive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, cookies = _client(monkeypatch, _compacting_turn)
        statuses = [d["status"] for n, d in _events(client, cookies) if n == "compaction_status"]
        assert statuses == ["start", "complete"]


class TestDrainMapsEveryKindAsThePollingLoopDoes:
    def test_every_kind_in_queue_order_before_the_final_event(
        self, monkeypatch: pytest.MonkeyPatch, done_first: None
    ) -> None:
        def send(cid: int, message: str, user_guid: str, **kwargs: Any) -> dict[str, Any]:
            cb = kwargs["status_callback"]
            cb("tool_call", {"tool_use_id": "t1", "tool_name": "web_fetch", "params": {"u": 1}})
            cb(
                "tool_result",
                {
                    "tool_use_id": "t1",
                    "tool_name": "web_fetch",
                    "result": "page",
                    "status": "success",
                    "inspection": INSPECTION,
                    "internal": "not forwarded",
                },
            )
            cb("tool_iteration_complete", {"iteration": 1})
            cb("agent_start", {"agent_id": "a1", "agent_name": "Researcher", "task": "t"})
            cb("agent_tool_call", {"agent_id": "a1", "tool_name": "web_fetch", "params": {}})
            cb(
                "agent_tool_result",
                {
                    "agent_id": "a1",
                    "tool_name": "web_fetch",
                    "result": "x",
                    "inspection": INSPECTION,
                },
            )
            cb("agent_complete", {"agent_id": "a1", "agent_name": "Researcher", "result": "ok"})
            cb("compaction_start", {"messages": 3})
            cb("compaction_complete", {"messages_compacted": 2})
            return _result()

        client, cookies = _client(monkeypatch, send)
        events = _events(client, cookies)
        assert [name for name, _ in events] == [
            "status",
            "tool_start",
            "tool_complete",
            "progress",
            "agent_start",
            "agent_tool_call",
            "agent_tool_result",
            "agent_complete",
            "compaction_status",
            "compaction_status",
            "response",
            "complete",
        ]
        by_name = {name: data for name, data in events}
        assert by_name["tool_start"] == {
            "tool_use_id": "t1",
            "tool_name": "web_fetch",
            "params": {"u": 1},
        }
        assert by_name["tool_complete"] == {
            "tool_use_id": "t1",
            "tool_name": "web_fetch",
            "result": "page",
            "status": "success",
            "inspection": INSPECTION,
        }
        assert by_name["agent_tool_result"]["inspection"] == INSPECTION
        assert by_name["agent_complete"]["status"] == "completed"
        assert events[-2][1]["final"] is True

    def test_a_permission_request_still_queued_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, done_first: None
    ) -> None:
        def send(cid: int, message: str, user_guid: str, **kwargs: Any) -> dict[str, Any]:
            kwargs["status_callback"]("permission_request", {"request_id": "r1"})
            return _result()

        client, cookies = _client(monkeypatch, send)
        names = [name for name, _ in _events(client, cookies)]
        assert names == ["status", "permission_request", "response", "complete"]


class TestExistingBehaviourIsUnchanged:
    def test_a_cancelled_turn_sends_cancelled_after_its_events(
        self, monkeypatch: pytest.MonkeyPatch, done_first: None
    ) -> None:
        def send(cid: int, message: str, user_guid: str, **kwargs: Any) -> dict[str, Any]:
            kwargs["status_callback"]("compaction_start", {})
            kwargs["cancel_token"].cancel("user")
            return {"status": "cancelled"}

        client, cookies = _client(monkeypatch, send)
        events = _events(client, cookies)
        assert [name for name, _ in events] == [
            "status",
            "compaction_status",
            "cancelled",
            "complete",
        ]
        assert events[2][1] == {"reason": "user"}

    def test_a_failing_turn_sends_an_error_and_no_events(
        self, monkeypatch: pytest.MonkeyPatch, done_first: None
    ) -> None:
        def send(cid: int, message: str, user_guid: str, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("model unavailable")

        client, cookies = _client(monkeypatch, send)
        events = _events(client, cookies)
        assert [name for name, _ in events] == ["status", "error"]
        assert events[1][1] == {"message": "model unavailable"}

    def test_events_during_a_running_turn_arrive_in_order_before_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = threading.Event()

        def send(cid: int, message: str, user_guid: str, **kwargs: Any) -> dict[str, Any]:
            cb = kwargs["status_callback"]
            cb("tool_call", {"tool_use_id": "t1", "tool_name": "x", "params": {}})
            seen.wait(timeout=0.6)  # at least one polling pass sees the call
            cb("compaction_complete", {"messages_compacted": 1})
            return _result()

        client, cookies = _client(monkeypatch, send)
        names = [name for name, _ in _events(client, cookies)]
        assert names == ["status", "tool_start", "compaction_status", "response", "complete"]
