"""The metering proxy: one usage row per model call, billed to the turn's user (A14)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest

from spark.database import usage
from spark.usage.meter import MeteredService, meter

PERIOD = datetime.now(timezone.utc).strftime("%Y-%m")


class StubService:
    """A provider stand-in: no network, fixed usage per call."""

    def __init__(self, usage_per_call: dict[str, int] | None = None) -> None:
        self.model = ""
        self.calls = 0
        self.usage = usage_per_call or {"input_tokens": 10, "output_tokens": 5}
        self._lock = threading.Lock()

    def set_model(self, model_id: str) -> None:
        self.model = model_id

    def count_tokens(self, text: str) -> int:
        return len(text)

    def invoke_model(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.calls += 1
        return {"content": "ok", "stop_reason": "end_turn", "usage": dict(self.usage)}


class FailingService(StubService):
    def invoke_model(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider down")


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT user_guid, model_id, source, conversation_id, action_id, input_tokens,"
        " output_tokens, period FROM usage_tracking ORDER BY id"
    )


class TestRecording:
    def test_one_row_per_call_for_the_turns_user(self, sync_db) -> None:
        service = meter(
            StubService(), sync_db, user_guid="u1", source="chat", model_id="m1", conversation_id=7
        )
        service.invoke_model([{"role": "user", "content": "hi"}])
        service.invoke_model([{"role": "user", "content": "again"}])
        rows = _rows(sync_db)
        assert len(rows) == 2
        assert rows[0] == {
            "user_guid": "u1",
            "model_id": "m1",
            "source": "chat",
            "conversation_id": 7,
            "action_id": None,
            "input_tokens": 10,
            "output_tokens": 5,
            "period": PERIOD,
        }

    def test_an_action_call_records_its_action(self, sync_db) -> None:
        service = meter(
            StubService(), sync_db, user_guid="owner", source="action", model_id="m", action_id=3
        )
        service.invoke_model([])
        [row] = _rows(sync_db)
        assert (row["user_guid"], row["source"], row["action_id"]) == ("owner", "action", 3)

    def test_cached_input_counts_as_input(self, sync_db) -> None:
        stub = StubService(
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
            }
        )
        meter(stub, sync_db, user_guid="u1", source="chat", model_id="m").invoke_model([])
        [row] = _rows(sync_db)
        assert (row["input_tokens"], row["output_tokens"]) == (130, 5)

    def test_the_model_set_on_the_service_is_the_one_recorded(self, sync_db) -> None:
        stub = StubService()
        service = meter(stub, sync_db, user_guid="u1", source="agent", model_id="m1")
        service.set_model("m2")
        service.invoke_model([])
        assert stub.model == "m2"
        assert _rows(sync_db)[0]["model_id"] == "m2"

    def test_a_response_without_usage_records_nothing(self, sync_db) -> None:
        stub = StubService()
        stub.usage = {}
        meter(stub, sync_db, user_guid="u1", source="chat", model_id="m").invoke_model([])
        assert _rows(sync_db) == []

    def test_a_failed_call_records_nothing_and_raises(self, sync_db) -> None:
        service = meter(FailingService(), sync_db, user_guid="u1", source="chat", model_id="m")
        with pytest.raises(RuntimeError, match="provider down"):
            service.invoke_model([])
        assert _rows(sync_db) == []

    def test_a_recording_failure_never_breaks_the_call(self, sync_db, monkeypatch, caplog) -> None:
        async def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database gone")

        monkeypatch.setattr(usage, "record_usage", broken)
        service = meter(StubService(), sync_db, user_guid="u1", source="chat", model_id="m")
        assert service.invoke_model([])["content"] == "ok"
        assert "usage" in caplog.text.lower()


class TestProxy:
    def test_other_attributes_are_the_services(self, sync_db) -> None:
        service = meter(StubService(), sync_db, user_guid="u1", source="chat", model_id="m")
        assert service.count_tokens("abcd") == 4

    def test_no_service_stays_none(self, sync_db) -> None:
        assert meter(None, sync_db, user_guid="u1", source="chat") is None

    def test_an_unknown_source_is_refused(self, sync_db) -> None:
        with pytest.raises(ValueError, match="source"):
            meter(StubService(), sync_db, user_guid="u1", source="billing")

    def test_a_metered_service_is_not_wrapped_twice(self, sync_db) -> None:
        once = meter(StubService(), sync_db, user_guid="u1", source="chat", model_id="m")
        twice = meter(once, sync_db, user_guid="u1", source="agent", model_id="m")
        assert isinstance(twice, MeteredService)
        twice.invoke_model([])
        [row] = _rows(sync_db)
        assert row["source"] == "agent"

    def test_with_source_shares_the_service(self, sync_db) -> None:
        stub = StubService()
        chat = meter(stub, sync_db, user_guid="u1", source="chat", model_id="m", conversation_id=1)
        chat.with_source("compaction").invoke_model([])
        [row] = _rows(sync_db)
        assert (row["source"], row["conversation_id"], row["user_guid"]) == ("compaction", 1, "u1")
        assert stub.calls == 1


@pytest.fixture()
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    """The bridge over a fresh database at head, on SQLite and (marked) PostgreSQL."""
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


class TestParallel:
    """Genuinely parallel calls for one user, from many threads at once: nothing is lost."""

    THREADS = 8
    CALLS = 25

    def test_parallel_calls_for_one_user_are_all_counted(self, bridge) -> None:
        stub = StubService({"input_tokens": 7, "output_tokens": 3})
        start = threading.Barrier(self.THREADS)

        def turn(n: int) -> None:
            service = meter(
                stub, bridge, user_guid="u1", source="chat", model_id="m", conversation_id=n
            )
            start.wait()
            for _ in range(self.CALLS):
                service.invoke_model([])

        with ThreadPoolExecutor(self.THREADS) as pool:
            list(pool.map(turn, range(self.THREADS)))

        expected = self.THREADS * self.CALLS * 10
        assert bridge.call(usage.tokens_in_period, "u1", PERIOD) == expected
        assert bridge.fetch_value("SELECT COUNT(*) FROM usage_tracking") == (
            self.THREADS * self.CALLS
        )
