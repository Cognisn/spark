"""The 0.2.x knowledge-graph status race, proved fixed with genuinely parallel work.

In 0.2.x the build thread and the status request shared one connection, so a
status count could read another thread's cursor (``fetchone()`` returned None).
Here builder threads write through the bridge while the event loop runs status
counts at the same time, on SQLite and on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from spark.db import Database, SyncDatabase
from spark.db.loop import LoopThread
from spark.knowledge import store

USER = "user-example-kg"


def test_builders_and_status_polls_in_parallel(spark_db_settings: Any) -> None:
    loop_thread = LoopThread(name="kg-race-loop")
    loop_thread.start()
    db: Database = loop_thread.run(Database.open(spark_db_settings, max_size=8))
    sync_db = SyncDatabase(db)
    try:
        builders = 4
        per_builder = 40
        start = threading.Barrier(builders + 1)
        done = threading.Event()

        def build(n: int) -> None:
            start.wait()
            for i in range(per_builder):
                node = sync_db.call(
                    store.upsert_node, "global", f"entity-{n}-{i}", "thing", "", None, USER
                )
                assert node > 0

        async def poll_status() -> int:
            polls = 0
            while not done.is_set():
                nodes, edges, builds = await asyncio.gather(
                    store.count_nodes(db, "global", USER),
                    store.count_edges(db, "global", USER),
                    store.get_build_state(db, "global", USER),
                )
                assert isinstance(nodes, int) and isinstance(edges, int)
                assert isinstance(builds, list)
                polls += 1
                await asyncio.sleep(0)
            return polls

        polls_future = asyncio.run_coroutine_threadsafe(poll_status(), loop_thread.loop)
        with ThreadPoolExecutor(max_workers=builders) as pool:
            futures = [pool.submit(build, n) for n in range(builders)]
            start.wait()
            for f in futures:
                f.result(timeout=120)
        done.set()
        assert polls_future.result(timeout=60) > 0
        assert sync_db.call(store.count_nodes, "global", USER) == builders * per_builder
    finally:
        loop_thread.run(db.close())
        loop_thread.stop()


def test_repeated_entities_merge_under_parallel_builders(spark_db_settings: Any) -> None:
    """Re-extracting the same entities from several threads bumps weight, never loses it."""
    loop_thread = LoopThread(name="kg-merge-loop")
    loop_thread.start()
    db: Database = loop_thread.run(Database.open(spark_db_settings, max_size=8))
    sync_db = SyncDatabase(db)
    try:
        # One node exists first, so parallel upserts all take the merge path.
        sync_db.call(store.upsert_node, "global", "Spark", "project", "", None, USER)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(
                pool.map(
                    lambda _: sync_db.call(
                        store.upsert_node, "global", "Spark", "project", "", None, USER
                    ),
                    range(30),
                )
            )
        nodes = sync_db.call(store.get_nodes, "global", USER)
        assert len(nodes) == 1
        assert nodes[0]["weight"] == 31
    finally:
        loop_thread.run(db.close())
        loop_thread.stop()
