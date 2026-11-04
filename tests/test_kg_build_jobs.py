"""Server mode: the worker builds queued knowledge graph jobs, each as its owner.

A real PostgreSQL clone at head and a real warden runtime hold synthetic owners:
a power user (who holds ``spark.knowledge.instance``), a plain user (who does
not), and a suspended power user. Model clients are scripted stubs and the
embedder is a fake, so nothing reaches a model.

Each job is built as the owner it names: the graph is the owner's, and
another user's graph is never read or written. The global scope needs
``spark.knowledge.instance``, read at run time. A refused, failed or
interrupted build is recorded on the job and can be retried; a running job
stops when its owner cancels it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import psycopg
import pytest
from warden.audit import set_recorder
from warden.identity import UserStatus

from spark import mode, worker
from spark.database import conversations, messages, user_identities
from spark.db import Database, SyncDatabase
from spark.identity.permissions import POWER_USER, USER
from spark.identity.runtime import IdentityRuntime
from spark.knowledge import build_jobs, jobs, store
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.test_identity_runtime import Konfig, _settings
from tests.test_instance_settings_web import MemorySecrets
from tests.test_kg_extractor import tool_response
from tests.test_kg_query import DirectionalEmbedder

pytestmark = pytest.mark.postgres

PUBLIC = "https://spark.example.com"


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    import spark.knowledge.tools as kg_tools

    monkeypatch.setattr(kg_tools, "_get_embedder", lambda: DirectionalEmbedder())


@pytest.fixture(autouse=True)
def clean_registrations() -> Iterator[None]:
    worker.clear_registrations()
    previous = set_recorder(None)
    yield
    worker.clear_registrations()
    set_recorder(previous)


@pytest.fixture
def server_db(pg_server: str, _pg_template: str) -> Iterator[DatabaseSettings]:
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=f"{base}/{name}")
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class Model:
    """A scripted model client: one fragment per chunk, naming ``entity``."""

    def __init__(self, entity: str = "Tea", on_call: Any = None) -> None:
        self.entity = entity
        self.on_call = on_call
        self.model: str | None = None
        self.calls = 0

    def set_model(self, model_id: str) -> None:
        self.model = model_id

    def invoke_model(self, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        return tool_response(
            [{"name": self.entity, "entity_type": "thing", "description": "made up"}], []
        )


class Clients:
    """Records each model a build asked for; hands out ``model`` (None: no provider)."""

    def __init__(self, model: Model | None) -> None:
        self.model = model
        self.asked: list[str] = []

    async def __call__(self, ctx: worker.WorkerContext) -> Any:
        def make(model_id: str) -> Any:
            self.asked.append(model_id)
            return self.model

        return make


@dataclass
class Stack:
    db: Database
    sync_db: SyncDatabase
    runtime: IdentityRuntime
    ctx: worker.WorkerContext
    guids: dict[str, str]


async def _owner(runtime: IdentityRuntime, db: Database, email: str, *roles: str) -> str:
    user = await runtime.identity.users.create(email, actor="test", password="dev-only-pass-1")
    for role in roles:
        await runtime.identity.roles.assign(user.id, role, actor="test")
    return await user_identities.user_guid_for(db, str(user.id))


@pytest.fixture
async def stack(server_db: DatabaseSettings) -> AsyncIterator[Stack]:
    mode.configure(RuntimeSettings(Mode.SERVER, server_db))
    db = await Database.open(server_db)
    runtime = await IdentityRuntime.start(db, _settings(), MemorySecrets())
    try:
        guids = {
            "ada": await _owner(runtime, db, "ada@example.com", POWER_USER),
            "grace": await _owner(runtime, db, "grace@example.com", USER),
            "suspended": await _owner(runtime, db, "gone@example.com", POWER_USER),
        }
        warden_id = await user_identities.warden_user_for(db, guids["suspended"])
        assert warden_id is not None
        await runtime.identity.users.set_status(
            uuid.UUID(warden_id), UserStatus.SUSPENDED, actor="test"
        )
        sync_db = SyncDatabase(db)
        ctx = worker.WorkerContext("worker-kg", db, sync_db, None, runtime)
        yield Stack(db, sync_db, runtime, ctx, guids)
    finally:
        runtime.close()
        await db.close()
        mode.reset()


async def _conversation(db: Database, guid: str, *texts: str) -> int:
    cid = int(await conversations.create_conversation(db, "c", "model-a", guid))
    for text in texts:
        await messages.add_message(db, cid, "user", text, 5, guid)
    return cid


async def _queue(db: Database, guid: str, scope: str, model: str = "model-a") -> jobs.BuildJob:
    job = await jobs.enqueue(db, guid, scope, model)
    assert job is not None
    return job


def _builds(clients: Clients, **kwargs: Any) -> build_jobs.GraphBuilds:
    return build_jobs.GraphBuilds(clients=clients, **kwargs)


async def _job(stack: Stack, guid: str, job_id: int) -> jobs.BuildJob:
    found = await jobs.get(stack.db, guid, job_id)
    assert found is not None
    return found


class TestBuildsAsTheOwner:
    async def test_a_queued_job_is_built_into_its_owners_graph(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        await _conversation(stack.db, ada, "Tea is a drink")
        job = await _queue(stack.db, ada, "global", "model-b")
        clients = Clients(Model("Tea"))

        await _builds(clients).run_once(stack.ctx)

        done = await _job(stack, ada, job.id)
        assert done.status == jobs.COMPLETE, done
        assert done.result is not None and done.result["entities"] == 1
        assert done.progress is not None and done.progress["chunks"] == 1
        assert clients.asked == ["model-b"]
        assert await store.count_nodes(stack.db, "global", ada) == 1
        assert await store.count_nodes(stack.db, "global", stack.guids["grace"]) == 0

    async def test_another_users_graph_is_never_read_or_written(self, stack: Stack) -> None:
        ada, grace = stack.guids["ada"], stack.guids["grace"]
        ada_cid = await _conversation(stack.db, ada, "Coffee is Ada's secret recipe")
        model = Model("Coffee")
        # Grace's job names Ada's conversation (the endpoint refuses this; the worker
        # must still build nothing from it).
        job = await _queue(stack.db, grace, f"conv:{ada_cid}")

        await _builds(Clients(model)).run_once(stack.ctx)

        done = await _job(stack, grace, job.id)
        assert done.status == jobs.COMPLETE
        assert model.calls == 0, "Ada's messages were sent to the model for Grace"
        assert await store.count_nodes(stack.db, f"conv:{ada_cid}", grace) == 0
        assert await store.count_nodes(stack.db, f"conv:{ada_cid}", ada) == 0
        assert await store.get_build_state(stack.db, f"conv:{ada_cid}", ada) == []

    async def test_jobs_of_several_owners_in_one_run(self, stack: Stack) -> None:
        ada, grace = stack.guids["ada"], stack.guids["grace"]
        await _conversation(stack.db, ada, "Tea is a drink")
        grace_cid = await _conversation(stack.db, grace, "Tea for Grace")
        first = await _queue(stack.db, ada, "global")
        second = await _queue(stack.db, grace, f"conv:{grace_cid}")

        await _builds(Clients(Model("Tea"))).run_once(stack.ctx)

        assert (await _job(stack, ada, first.id)).status == jobs.COMPLETE
        assert (await _job(stack, grace, second.id)).status == jobs.COMPLETE
        assert await store.count_nodes(stack.db, "global", ada) == 1
        assert await store.count_nodes(stack.db, f"conv:{grace_cid}", grace) == 1
        assert await store.count_nodes(stack.db, "global", grace) == 0


class TestRefusals:
    async def test_the_global_graph_needs_the_instance_permission(self, stack: Stack) -> None:
        grace = stack.guids["grace"]
        cid = await _conversation(stack.db, grace, "Tea is a drink")
        refused = await _queue(stack.db, grace, "global")
        allowed = await _queue(stack.db, grace, f"conv:{cid}")
        model = Model("Tea")

        await _builds(Clients(model)).run_once(stack.ctx)

        found = await _job(stack, grace, refused.id)
        assert found.status == jobs.FAILED
        assert found.error is not None and "spark.knowledge.instance" in found.error
        assert await store.count_nodes(stack.db, "global", grace) == 0
        assert (await _job(stack, grace, allowed.id)).status == jobs.COMPLETE
        assert model.calls == 1

    async def test_a_suspended_owner_is_refused(self, stack: Stack) -> None:
        gone = stack.guids["suspended"]
        await _conversation(stack.db, gone, "Tea is a drink")
        job = await _queue(stack.db, gone, "global")
        model = Model()

        await _builds(Clients(model)).run_once(stack.ctx)

        found = await _job(stack, gone, job.id)
        assert found.status == jobs.FAILED
        assert found.error is not None and "suspended" in found.error
        assert model.calls == 0

    async def test_a_refusal_is_audited(self, stack: Stack) -> None:
        from warden.audit import AuditQuery, list_events

        from spark.identity import audit
        from spark.identity.db import warden_database

        grace = stack.guids["grace"]
        job = await _queue(stack.db, grace, "global")
        async with audit.installed(stack.db):
            await _builds(Clients(Model())).run_once(stack.ctx)

        page = await list_events(
            warden_database(stack.db), AuditQuery(actions=(audit.KNOWLEDGE_BUILD_REFUSED,))
        )
        [row] = page.rows
        assert row.outcome == "failure"
        assert row.resource_id == "global"
        assert row.details["job_id"] == job.id
        assert row.details["user_guid"] == grace

    async def test_a_worker_without_warden_leaves_jobs_queued(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        job = await _queue(stack.db, ada, "global")
        bare = worker.WorkerContext("worker-kg", stack.db, stack.sync_db, None, None)
        with pytest.raises(worker.WorkerRefused):
            await _builds(Clients(Model())).run_once(bare)
        assert (await _job(stack, ada, job.id)).status == jobs.QUEUED


class TestFailureAndRetry:
    async def test_no_model_client_fails_and_a_retry_builds(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        await _conversation(stack.db, ada, "Tea is a drink")
        job = await _queue(stack.db, ada, "global", "gone-model")

        await _builds(Clients(None)).run_once(stack.ctx)

        failed = await _job(stack, ada, job.id)
        assert failed.status == jobs.FAILED
        assert failed.error is not None and "gone-model" in failed.error

        outcome, _ = await jobs.retry(stack.db, ada, job.id)
        assert outcome == "queued"
        await _builds(Clients(Model("Tea"))).run_once(stack.ctx)

        done = await _job(stack, ada, job.id)
        assert done.status == jobs.COMPLETE and done.attempts == 2
        assert await store.count_nodes(stack.db, "global", ada) == 1

    async def test_a_build_that_raises_is_recorded_as_failed(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        await _conversation(stack.db, ada, "Tea is a drink")
        job = await _queue(stack.db, ada, "global")

        class Broken(Clients):
            async def __call__(self, ctx: worker.WorkerContext) -> Any:
                def make(model_id: str) -> Any:
                    raise RuntimeError("provider set-up failed")

                return make

        await _builds(Broken(None)).run_once(stack.ctx)

        failed = await _job(stack, ada, job.id)
        assert failed.status == jobs.FAILED
        assert failed.error is not None and "RuntimeError" in failed.error

    async def test_a_job_left_running_by_a_stopped_worker_is_failed(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        job = await _queue(stack.db, ada, "global")
        assert await jobs.claim_next(stack.db, "worker-gone") is not None
        await stack.db.execute(
            "UPDATE kg_build_jobs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job.id,),
        )

        await _builds(Clients(Model()), stale_seconds=60).run_once(stack.ctx)

        found = await _job(stack, ada, job.id)
        assert found.status == jobs.FAILED and found.error == jobs.STALE_ERROR


class TestCancel:
    async def test_a_running_build_stops_when_its_owner_cancels(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        # Two messages of 7000 characters make two chunks.
        await _conversation(stack.db, ada, "tea " * 1750, "cake " * 1400)
        job = await _queue(stack.db, ada, "global")

        def cancel_on_first(calls: int) -> None:
            if calls == 1:
                assert stack.sync_db.call(jobs.request_cancel, ada, "global") == "cancelling"

        model = Model("Tea", on_call=cancel_on_first)
        await _builds(Clients(model)).run_once(stack.ctx)

        found = await _job(stack, ada, job.id)
        assert found.status == jobs.CANCELLED
        assert model.calls == 1

    async def test_a_build_whose_job_went_stale_stops(self, stack: Stack) -> None:
        ada = stack.guids["ada"]
        await _conversation(stack.db, ada, "tea " * 1750, "cake " * 1400)
        job = await _queue(stack.db, ada, "global")

        async def fail(db: Any, job_id: int) -> None:
            await db.execute("UPDATE kg_build_jobs SET status = 'failed' WHERE id = ?", (job_id,))

        def go_stale_on_first(calls: int) -> None:
            if calls == 1:
                stack.sync_db.call(fail, job.id)

        model = Model("Tea", on_call=go_stale_on_first)
        await _builds(Clients(model)).run_once(stack.ctx)

        assert model.calls == 1, "the build went on after its job stopped being its own"
        assert (await _job(stack, ada, job.id)).status == jobs.FAILED


class TestRegistration:
    def test_registered_as_a_periodic_job(self) -> None:
        build_jobs.register(Konfig({"worker": {"knowledge_graph_poll_seconds": 2}}))
        [job] = worker.registered_periodic()
        assert (job.name, job.every_seconds) == (build_jobs.JOB_NAME, 2.0)

    def test_registering_twice_is_harmless(self) -> None:
        build_jobs.register(Konfig({}))
        build_jobs.register(Konfig({}))
        [job] = worker.registered_periodic()
        assert job.every_seconds == build_jobs.DEFAULT_POLL_SECONDS

    def test_spark_worker_registers_the_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeWorker:
            def __init__(self, settings: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def run(self, stop: asyncio.Event) -> None:
                return None

        class App:
            settings = Konfig({"server": {"public_base_url": PUBLIC}, "warden": {}})
            secrets = MemorySecrets()

        def with_app(body: Any) -> tuple[Any, Any]:
            return None, asyncio.run(body(App()))

        monkeypatch.setattr(worker, "Worker", FakeWorker)
        monkeypatch.setattr(worker, "_with_app", with_app)
        runtime = RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y")
        )
        worker._serve(runtime)
        assert build_jobs.JOB_NAME in [job.name for job in captured["periodic"]]


class TestTheWorkerRunsThem:
    async def test_an_active_worker_builds_a_queued_job(
        self, stack: Stack, server_db: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ada = stack.guids["ada"]
        await _conversation(stack.db, ada, "Tea is a drink")
        job = await _queue(stack.db, ada, "global")

        # No konfig app in tests: the active worker uses this test's warden runtime.
        @asynccontextmanager
        async def warden(ctx: worker.WorkerContext) -> AsyncIterator[worker.WorkerContext]:
            yield replace(ctx, identity=stack.runtime)

        monkeypatch.setattr(worker, "_warden", warden)
        running = worker.Worker(
            server_db,
            worker_id="worker-kg-live",
            periodic=[worker.PeriodicJob(build_jobs.JOB_NAME, 0.1, _builds(Clients(Model())))],
            standby_seconds=0.1,
            check_seconds=0.1,
            takeover_grace=0.1,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(running.run(stop))
        try:
            for _ in range(200):
                if (await _job(stack, ada, job.id)).status == jobs.COMPLETE:
                    break
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, 20)
        found = await _job(stack, ada, job.id)
        assert found.status == jobs.COMPLETE, found
        assert found.claimed_by == "worker-kg-live"
