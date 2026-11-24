"""The live bus of shared conversations: in-process, and across replicas over PostgreSQL.

Locally (personal mode, SQLite) an event reaches the replica's own subscribers. On a
server every replica runs its own bus, each with its own LISTEN connection, and an
event one replica publishes reaches the pages on every other. A removal closes the
removed person's views at once and cancels their turn where it runs; a deletion
closes every view.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg
import pytest

from spark.web.live import CHANNEL, MAX_PAYLOAD, LiveBus, TurnHandle, split_text


async def _next(sub: Any, timeout: float = 5.0) -> dict[str, Any]:
    item: dict[str, Any] = await asyncio.wait_for(sub.queue.get(), timeout)
    return item


class TestLocal:
    async def test_events_reach_the_conversations_watchers_only(self) -> None:
        bus = LiveBus()
        mine = bus.subscribe(1, "vic")
        other = bus.subscribe(2, "vic")
        await bus.publish(1, "turn_started", {"user_guid": "ada", "name": "Ada"})
        assert await _next(mine) == {
            "event": "turn_started",
            "data": {"user_guid": "ada", "name": "Ada"},
        }
        assert other.queue.empty()
        assert bus.watchers(1) == 1
        bus.unsubscribe(mine)
        assert bus.watchers(1) == 0

    async def test_removal_closes_that_persons_views_and_cancels_their_turn(self) -> None:
        bus = LiveBus()
        vic = bus.subscribe(1, "vic")
        ada = bus.subscribe(1, "ada")
        cancelled: list[str] = []
        turn = TurnHandle(1, "vic", cancel=cancelled.append)
        bus.register_turn(turn)
        await bus.publish(1, "member_removed", {"user_guid": "vic"})
        assert (await _next(vic))["event"] == "removed"
        assert vic.closed.is_set()
        assert (await _next(ada))["event"] == "member_removed"
        assert not ada.closed.is_set()
        assert cancelled == ["removed"] and turn.removed.is_set()

    async def test_removal_of_someone_else_leaves_a_turn_alone(self) -> None:
        bus = LiveBus()
        cancelled: list[str] = []
        bus.register_turn(TurnHandle(1, "ada", cancel=cancelled.append))
        await bus.publish(1, "member_removed", {"user_guid": "vic"})
        assert cancelled == []

    async def test_deletion_closes_every_view(self) -> None:
        bus = LiveBus()
        subs = [bus.subscribe(1, who) for who in ("ada", "vic")]
        await bus.publish(1, "conversation_deleted", {})
        for sub in subs:
            assert (await _next(sub))["event"] == "deleted"
            assert sub.closed.is_set()

    async def test_a_large_delta_is_split(self) -> None:
        bus = LiveBus()
        sub = bus.subscribe(1, "vic")
        text = "é" * MAX_PAYLOAD
        await bus.publish(1, "delta", {"seq": 1, "text": text})
        pieces = []
        while not sub.queue.empty():
            pieces.append((await _next(sub))["data"]["text"])
        assert len(pieces) > 1 and "".join(pieces) == text


def test_split_text_keeps_characters_whole() -> None:
    pieces = split_text("aé" * 10, limit=5)
    assert "".join(pieces) == "aé" * 10
    assert all(len(p.encode("utf-8")) <= 5 for p in pieces)
    assert split_text("") == []


@pytest.mark.postgres
class TestAcrossReplicas:
    async def _buses(self, pg_url: str) -> tuple[LiveBus, LiveBus, Any]:
        conn = await psycopg.AsyncConnection.connect(pg_url, autocommit=True)

        async def notify(payload: str) -> None:
            await conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))

        one = LiveBus(notify=notify, listen_url=pg_url, idle_timeout=0.5)
        two = LiveBus(notify=notify, listen_url=pg_url, idle_timeout=0.5)
        return one, two, conn

    async def test_an_event_reaches_the_other_replica(self, pg_url: str) -> None:
        one, two, conn = await self._buses(pg_url)
        try:
            watcher = two.subscribe(7, "vic")
            await asyncio.wait_for(two.listening.wait(), 10)
            await one.publish(7, "delta", {"seq": 1, "text": "hello"})
            got = await _next(watcher)
            assert got == {"event": "delta", "data": {"seq": 1, "text": "hello"}}
        finally:
            await one.close()
            await two.close()
            await conn.close()

    async def test_a_removal_on_one_replica_cancels_the_turn_on_another(self, pg_url: str) -> None:
        one, two, conn = await self._buses(pg_url)
        try:
            cancelled: list[str] = []
            two.register_turn(TurnHandle(7, "cora", cancel=cancelled.append))
            await asyncio.wait_for(two.listening.wait(), 10)
            await one.publish(7, "member_removed", {"user_guid": "cora"})
            for _ in range(50):
                if cancelled:
                    break
                await asyncio.sleep(0.1)
            assert cancelled == ["removed"]
        finally:
            await one.close()
            await two.close()
            await conn.close()

    async def test_the_listener_stops_when_nobody_watches(self, pg_url: str) -> None:
        one, _, conn = await self._buses(pg_url)
        try:
            sub = one.subscribe(7, "vic")
            await asyncio.wait_for(one.listening.wait(), 10)
            one.unsubscribe(sub)
            await asyncio.sleep(0.2)
            async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as check:
                rows = await (
                    await check.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
                        ("spark-conversation-listener",),
                    )
                ).fetchone()
            assert rows is not None and rows[0] == 0
        finally:
            await one.close()
            await conn.close()

    async def test_a_payload_stays_under_the_notify_limit(self, pg_url: str) -> None:
        one, two, conn = await self._buses(pg_url)
        try:
            watcher = two.subscribe(7, "vic")
            await asyncio.wait_for(two.listening.wait(), 10)
            text = "x" * 20000
            await one.publish(7, "delta", {"seq": 1, "text": text})
            got = []
            while len("".join(got)) < len(text):
                got.append((await _next(watcher))["data"]["text"])
            assert "".join(got) == text
            assert all(len(json.dumps(g)) < MAX_PAYLOAD for g in got)
        finally:
            await one.close()
            await two.close()
            await conn.close()
