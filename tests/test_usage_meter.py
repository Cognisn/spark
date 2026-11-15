"""Metering through conduit's events: one usage row per model call, billed to the turn's user,
on the connection it ran on, with the four token counts apart (A14, M6)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest
from conduit import ProviderError, ProviderFailure
from conduit.providers.stub import StubTurn, scripted

from spark.connections import ConnectionRuntime
from spark.database import usage
from spark.usage.meter import meter
from tests.model_helpers import stub_connection, stub_runtime

PERIOD = datetime.now(timezone.utc).strftime("%Y-%m")
MODEL = "stub/stub-echo"


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT user_guid, connection_id, model_id, source, conversation_id, action_id,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, period"
        " FROM usage_tracking ORDER BY id"
    )


@pytest.fixture
def runtime(sync_db: Any) -> Iterator[ConnectionRuntime]:
    rt = stub_runtime(sync_db)
    try:
        yield rt
    finally:
        rt.close()


class TestRecording:
    def test_one_row_per_call_for_the_turns_user(self, sync_db: Any, runtime: Any) -> None:
        service = meter(
            runtime.service(MODEL), sync_db, user_guid="u1", source="chat", conversation_id=7
        )
        first = service.invoke_model([{"role": "user", "content": "hi"}])
        service.invoke_model([{"role": "user", "content": "again"}])
        rows = _rows(sync_db)
        assert len(rows) == 2
        assert rows[0] == {
            "user_guid": "u1",
            "connection_id": "stub",
            "model_id": "stub-echo",
            "source": "chat",
            "conversation_id": 7,
            "action_id": None,
            "input_tokens": first["usage"]["input_tokens"],
            "output_tokens": first["usage"]["output_tokens"],
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "period": PERIOD,
        }

    def test_an_action_call_records_its_action(self, sync_db: Any, runtime: Any) -> None:
        service = meter(runtime.service(MODEL), user_guid="owner", source="action", action_id=3)
        service.invoke_model([{"role": "user", "content": "x"}])
        [row] = _rows(sync_db)
        assert (row["user_guid"], row["source"], row["action_id"]) == ("owner", "action", 3)

    def test_cache_counts_kept_apart_and_in_the_total(self, sync_db: Any, runtime: Any) -> None:
        service = meter(runtime.service(MODEL), user_guid="u1", source="chat")
        system = "A stable system prompt for example.org, long enough to cache."
        written = service.invoke_model(
            [{"role": "user", "content": "one"}], system=system, prompt_caching=True
        )
        read = service.invoke_model(
            [{"role": "user", "content": "two"}], system=system, prompt_caching=True
        )
        first, second = _rows(sync_db)
        assert first["cache_write_tokens"] == written["usage"]["cache_creation_input_tokens"] > 0
        assert second["cache_read_tokens"] == read["usage"]["cache_read_input_tokens"] > 0
        total = sum(
            r["input_tokens"]
            + r["output_tokens"]
            + r["cache_read_tokens"]
            + r["cache_write_tokens"]
            for r in (first, second)
        )
        assert sync_db.call(usage.tokens_in_period, "u1", PERIOD) == total

    def test_another_model_on_the_service_is_the_one_recorded(
        self, sync_db: Any, runtime: Any
    ) -> None:
        service = meter(runtime.service(MODEL), user_guid="u1", source="agent")
        service.set_model("stub/stub-echo")
        service.invoke_model([{"role": "user", "content": "x"}])
        assert _rows(sync_db)[0]["model_id"] == "stub-echo"

    def test_unattributed_calls_record_nothing(self, sync_db: Any, runtime: Any) -> None:
        runtime.service(MODEL).invoke_model([{"role": "user", "content": "x"}])
        assert _rows(sync_db) == []

    def test_a_failed_call_records_nothing_and_raises(self, sync_db: Any) -> None:
        from spark.connections import ModelCallFailed

        failing = scripted([ProviderError(ProviderFailure.REJECTED)])
        rt = stub_runtime(
            sync_db, connections=[stub_connection(script="fail")], scripts={"fail": failing}
        )
        try:
            service = meter(rt.service(MODEL), user_guid="u1", source="chat")
            with pytest.raises(ModelCallFailed):
                service.invoke_model([{"role": "user", "content": "x"}])
        finally:
            rt.close()
        assert _rows(sync_db) == []

    def test_a_recording_failure_never_breaks_the_call(
        self, sync_db: Any, runtime: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        async def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database gone")

        monkeypatch.setattr(usage, "record_usage", broken)
        service = meter(runtime.service(MODEL), user_guid="u1", source="chat")
        assert service.invoke_model([{"role": "user", "content": "ok"}])["content"] == "ok"
        assert "usage" in caplog.text.lower()


class TestAttribution:
    def test_no_service_stays_none(self) -> None:
        assert meter(None, user_guid="u1", source="chat") is None

    def test_an_unknown_source_is_refused(self, runtime: Any) -> None:
        with pytest.raises(ValueError, match="source"):
            meter(runtime.service(MODEL), user_guid="u1", source="billing")

    def test_a_service_that_cannot_be_attributed_is_returned_as_it_is(self) -> None:
        other = object()
        assert meter(other, user_guid="u1", source="chat") is other

    def test_attributing_again_relabels(self, sync_db: Any, runtime: Any) -> None:
        once = meter(runtime.service(MODEL), user_guid="u1", source="chat")
        twice = meter(once, user_guid="u1", source="agent")
        twice.invoke_model([{"role": "user", "content": "x"}])
        [row] = _rows(sync_db)
        assert row["source"] == "agent"

    def test_with_source_keeps_user_and_conversation(self, sync_db: Any, runtime: Any) -> None:
        chat = meter(runtime.service(MODEL), user_guid="u1", source="chat", conversation_id=1)
        chat.with_source("compaction").invoke_model([{"role": "user", "content": "x"}])
        [row] = _rows(sync_db)
        assert (row["source"], row["conversation_id"], row["user_guid"]) == ("compaction", 1, "u1")


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
    CALLS = 10

    def test_parallel_calls_for_one_user_are_all_counted(self, bridge: Any) -> None:
        from conduit import Usage

        fixed = StubTurn(
            text="ok",
            usage=Usage(input_tokens=7, output_tokens=3, cache_read_tokens=0, cache_write_tokens=0),
        )
        rt = stub_runtime(
            bridge,
            connections=[stub_connection(script="fixed")],
            scripts={"fixed": lambda request, index: fixed},
        )
        start = threading.Barrier(self.THREADS)

        def turn(n: int) -> None:
            service = meter(rt.service(MODEL), user_guid="u1", source="chat", conversation_id=n)
            start.wait()
            for _ in range(self.CALLS):
                service.invoke_model([{"role": "user", "content": "x"}])

        try:
            with ThreadPoolExecutor(self.THREADS) as pool:
                list(pool.map(turn, range(self.THREADS)))
        finally:
            rt.close()
        assert bridge.call(usage.tokens_in_period, "u1", PERIOD) == self.THREADS * self.CALLS * 10
        assert bridge.fetch_value("SELECT COUNT(*) FROM usage_tracking") == (
            self.THREADS * self.CALLS
        )
