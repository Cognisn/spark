"""A running turn's interactions live as long as the turn, not as long as its page.

:class:`spark.web.interactions.TurnRecord` holds a turn's pending tool permission
prompts and agent model approvals, and its turn and agent cancel tokens, from the
moment the turn starts until it ends, however it ends, each bound to the sender's
guid. A held prompt waits :data:`~spark.web.interactions.AWAY_LIMIT` seconds while no
page of the sender's has the conversation open, and
:data:`~spark.web.interactions.SEEN_LIMIT` seconds from the moment one is (Matthew,
28 November 2026). The live bus sends a prompt only to its sender's pages, and carries
answers and cancels to the replica running the turn as control messages no page sees.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from spark.core.cancellation import CancellationToken
from spark.web import interactions
from spark.web.interactions import AGENT, AGENT_MODEL, PERMISSION, STREAM, TurnRecord
from spark.web.live import LiveBus, TurnHandle

ADA = "user-ada-example"
VIC = "user-vic-example"


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


def _record(app: Any, published: list[tuple[str, dict[str, Any]]] | None = None) -> TurnRecord:
    sink = published if published is not None else []
    record = TurnRecord(
        app, 7, ADA, "stream-1", CancellationToken(), publish=lambda e, d: sink.append((e, d))
    )
    record.open()
    return record


def _ask_in_thread(record: TurnRecord, kind: str, rid: str) -> tuple[threading.Thread, dict]:
    out: dict[str, Any] = {}

    def run() -> None:
        started = time.monotonic()
        out["answer"] = record.ask(kind, rid, {"tool_name": "web_search", "params": {"q": "x"}})
        out["took"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def _wait(check: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
def short_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interactions, "SEEN_LIMIT", 0.4)
    monkeypatch.setattr(interactions, "AWAY_LIMIT", 1.2)
    monkeypatch.setattr(interactions, "PRESENCE_FRESH", 0.3)
    monkeypatch.setattr(interactions, "WAIT_STEP", 0.02)


class TestLifetime:
    def test_open_registers_the_turn_bound_to_its_sender(self) -> None:
        app = _app()
        record = _record(app)
        owners = app.state.interaction_owners
        assert owners[(STREAM, "stream-1")] == ADA
        assert app.state.turn_cancel_tokens["stream-1"] is record.token
        assert interactions.record_for_stream(app, "stream-1") is record
        assert interactions.record_for_conversation(app, 7) is record

    def test_agents_are_bound_and_cancelled_with_the_turn(self) -> None:
        app = _app()
        record = _record(app)
        one = record.register_agent("agent-1", "Researcher")
        two = record.register_agent("agent-2", "Writer")
        assert app.state.interaction_owners[(AGENT, "agent-1")] == ADA
        assert app.state.agent_cancel_tokens["agent-2"] is two
        record.cancel("user")
        assert record.token.is_cancelled() and one.is_cancelled() and two.is_cancelled()

    def test_close_releases_everything_and_ends_a_waiting_prompt(self, short_limits: None) -> None:
        app = _app()
        record = _record(app)
        record.register_agent("agent-1")
        thread, out = _ask_in_thread(record, PERMISSION, "p1")
        _wait(lambda: "p1" in app.state.permission_events)
        record.close()
        thread.join(5)
        assert out["answer"] is None
        assert app.state.interaction_owners == {}
        assert app.state.turn_cancel_tokens == {}
        assert app.state.agent_cancel_tokens == {}
        assert app.state.permission_events == {}
        assert interactions.record_for_stream(app, "stream-1") is None
        record.close()  # once is enough; a second close changes nothing


class TestPrompts:
    def test_a_prompt_is_offered_to_the_sender_and_answered(self) -> None:
        app = _app()
        published: list[tuple[str, dict[str, Any]]] = []
        record = _record(app, published)
        thread, out = _ask_in_thread(record, PERMISSION, "p1")
        _wait(lambda: published)
        assert published[0] == (
            "prompt",
            {
                "kind": PERMISSION,
                "request_id": "p1",
                "stream_id": "stream-1",
                "tool_name": "web_search",
                "params": {"q": "x"},
            },
        )
        assert app.state.interaction_owners[(PERMISSION, "p1")] == ADA
        assert [p["request_id"] for p in record.snapshot()["prompts"]] == ["p1"]
        assert record.answer(PERMISSION, "p1", "once")
        thread.join(5)
        assert out["answer"] == "once"
        assert published[-1] == ("prompt_closed", {"request_id": "p1", "stream_id": "stream-1"})
        assert record.snapshot()["prompts"] == []
        assert (PERMISSION, "p1") not in app.state.interaction_owners

    def test_an_answer_of_another_kind_or_id_is_refused(self) -> None:
        app = _app()
        record = _record(app)
        thread, out = _ask_in_thread(record, AGENT_MODEL, "m1")
        _wait(lambda: "m1" in app.state.agent_model_events)
        assert not record.answer(PERMISSION, "m1", "allowed")
        assert not record.answer(AGENT_MODEL, "nope", "model-b")
        assert record.answer(AGENT_MODEL, "m1", "model-b")
        thread.join(5)
        assert out["answer"] == "model-b"

    def test_while_no_page_is_open_it_waits_the_away_limit(self, short_limits: None) -> None:
        record = _record(_app())
        thread, out = _ask_in_thread(record, PERMISSION, "p1")
        thread.join(5)
        assert out["answer"] is None
        assert out["took"] >= 1.2 - 0.05

    def test_with_a_page_open_it_waits_the_seen_limit(self, short_limits: None) -> None:
        record = _record(_app())
        record.present()
        thread, out = _ask_in_thread(record, PERMISSION, "p1")
        thread.join(5)
        assert out["answer"] is None
        assert 0.4 - 0.05 <= out["took"] < 1.0

    def test_a_page_that_opens_later_gets_the_seen_limit_from_then(
        self, short_limits: None
    ) -> None:
        record = _record(_app())
        thread, out = _ask_in_thread(record, PERMISSION, "p1")
        time.sleep(0.5)  # away: past the seen limit already, but still waiting
        assert thread.is_alive()
        record.present()
        thread.join(5)
        assert out["answer"] is None
        assert 0.9 - 0.05 <= out["took"] < 1.2

    def test_a_cancelled_turn_ends_its_prompt_at_once(self, short_limits: None) -> None:
        record = _record(_app())
        thread, out = _ask_in_thread(record, PERMISSION, "p1")
        time.sleep(0.1)
        record.cancel("user")
        thread.join(5)
        assert out["answer"] is None and out["took"] < 0.5

    def test_the_snapshot_names_the_running_agents_and_activity(self) -> None:
        record = _record(_app())
        record.register_agent("agent-1", "Researcher")
        record.activity = {"kind": "tool", "tool_name": "web_search"}
        snap = record.snapshot()
        assert snap["agents"] == [{"agent_id": "agent-1", "agent_name": "Researcher"}]
        assert snap["activity"] == {"kind": "tool", "tool_name": "web_search"}
        assert snap["stream_id"] == "stream-1" and snap["started_at"]
        record.agent_done("agent-1")
        assert record.snapshot()["agents"] == []


async def _next(sub: Any, timeout: float = 5.0) -> dict[str, Any]:
    item: dict[str, Any] = await asyncio.wait_for(sub.queue.get(), timeout)
    return item


class TestBus:
    async def test_a_private_event_reaches_only_its_persons_pages(self) -> None:
        bus = LiveBus()
        ada = bus.subscribe(7, ADA)
        vic = bus.subscribe(7, VIC)
        await bus.publish(7, "prompt", {"request_id": "p1"}, to=ADA)
        assert await _next(ada) == {"event": "prompt", "data": {"request_id": "p1"}}
        assert vic.queue.empty()

    async def test_a_control_message_reaches_the_turn_and_no_page(self) -> None:
        bus = LiveBus()
        ada = bus.subscribe(7, ADA)
        seen: list[tuple[str, dict[str, Any]]] = []
        bus.register_turn(
            TurnHandle(
                7,
                ADA,
                cancel=lambda r: None,
                stream_id="s1",
                control=lambda e, d: seen.append((e, d)),
            )
        )
        await bus.publish_control(7, "cancel", {"user_guid": ADA, "stream_id": "s1"})
        assert seen == [("cancel", {"user_guid": ADA, "stream_id": "s1"})]
        assert ada.queue.empty()

    async def test_a_control_message_for_someone_else_or_another_turn_is_ignored(self) -> None:
        bus = LiveBus()
        seen: list[str] = []
        bus.register_turn(
            TurnHandle(
                7, ADA, cancel=lambda r: None, stream_id="s1", control=lambda e, d: seen.append(e)
            )
        )
        await bus.publish_control(7, "cancel", {"user_guid": VIC, "stream_id": "s1"})
        await bus.publish_control(7, "cancel", {"user_guid": ADA, "stream_id": "s2"})
        await bus.publish_control(8, "cancel", {"user_guid": ADA, "stream_id": "s1"})
        assert seen == []

    async def test_the_turns_on_this_replica_are_known(self) -> None:
        bus = LiveBus()
        handle = TurnHandle(7, ADA, cancel=lambda r: None, stream_id="s1")
        bus.register_turn(handle)
        assert bus.turn(7) is handle and bus.turn(8) is None
        assert bus.turns_of(ADA) == [7] and bus.turns_of(VIC) == []
        bus.unregister_turn(handle)
        assert bus.turn(7) is None

    async def test_a_large_turn_text_is_split(self) -> None:
        from spark.web.live import MAX_PAYLOAD

        bus = LiveBus()
        sub = bus.subscribe(7, ADA)
        text = "x" * (MAX_PAYLOAD * 2)
        await bus.publish(7, "turn_text", {"stream_id": "s1", "text": text}, to=ADA)
        pieces = []
        while not sub.queue.empty():
            pieces.append((await _next(sub))["data"]["text"])
        assert len(pieces) > 1 and "".join(pieces) == text


@pytest.mark.postgres
class TestControlAcrossReplicas:
    async def test_a_cancel_published_on_one_replica_reaches_the_turn_on_another(
        self, pg_url: str
    ) -> None:
        import psycopg

        from spark.web.live import CHANNEL

        conn = await psycopg.AsyncConnection.connect(pg_url, autocommit=True)

        async def notify(payload: str) -> None:
            await conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))

        one = LiveBus(notify=notify, listen_url=pg_url, idle_timeout=0.5)
        two = LiveBus(notify=notify, listen_url=pg_url, idle_timeout=0.5)
        try:
            seen: list[tuple[str, dict[str, Any]]] = []
            two.register_turn(
                TurnHandle(
                    7,
                    ADA,
                    cancel=lambda r: None,
                    stream_id="s1",
                    control=lambda e, d: seen.append((e, d)),
                )
            )
            await asyncio.wait_for(two.listening.wait(), 10)
            await one.publish_control(
                7, "answer", {"user_guid": ADA, "stream_id": "s1", "value": "once"}
            )
            for _ in range(50):
                if seen:
                    break
                await asyncio.sleep(0.1)
            assert seen == [("answer", {"user_guid": ADA, "stream_id": "s1", "value": "once"})]
        finally:
            await one.close()
            await two.close()
            await conn.close()
