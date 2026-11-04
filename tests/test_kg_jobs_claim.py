"""Two genuinely parallel workers claim build jobs on PostgreSQL: each job once (A17).

Each worker runs on its own thread with its own event loop and its own
connection pool, so their claims reach the server at the same time. Both
start together from a barrier and claim until the queue is empty; every job
is claimed by exactly one of them, and both do some of the work.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any

import pytest

from spark.db import Database
from spark.knowledge import jobs
from spark.mode import DatabaseSettings

pytestmark = pytest.mark.postgres

JOBS = 120


def _loop() -> asyncio.AbstractEventLoop:
    # psycopg's async driver needs the Selector loop on Windows.
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def _run(coro: Any) -> Any:
    loop = _loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed(settings: DatabaseSettings) -> set[int]:
    db = await Database.open(settings)
    try:
        ids: set[int] = set()
        for n in range(JOBS):
            job = await jobs.enqueue(db, f"user-{n % 7}", f"conv:{n}", "model-a")
            assert job is not None
            ids.add(job.id)
        return ids
    finally:
        await db.close()


async def _work(
    settings: DatabaseSettings, worker_id: str, barrier: threading.Barrier, claimed: list[int]
) -> None:
    db = await Database.open(settings)
    try:
        await asyncio.to_thread(barrier.wait)
        while True:
            job = await jobs.claim_next(db, worker_id)
            if job is None:
                return
            claimed.append(job.id)
            assert await jobs.record_progress(db, job.id, worker_id, {"chunks": 1})
            assert await jobs.finish(db, job.id, worker_id, jobs.COMPLETE, result={})
    finally:
        await db.close()


def test_two_parallel_workers_claim_each_job_once(spark_db_settings: DatabaseSettings) -> None:
    if spark_db_settings.url is None:
        pytest.skip("PostgreSQL only: SQLite has one writer at a time")
    expected = _run(_seed(spark_db_settings))
    barrier = threading.Barrier(2)
    claimed: dict[str, list[int]] = {"w1": [], "w2": []}
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            _run(_work(spark_db_settings, name, barrier, claimed[name]))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in claimed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    every = claimed["w1"] + claimed["w2"]
    assert sorted(every) == sorted(expected), "each job is claimed exactly once"
    assert claimed["w1"] and claimed["w2"], "both workers claimed jobs in parallel"
