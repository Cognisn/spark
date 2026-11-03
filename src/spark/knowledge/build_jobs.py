"""The worker's knowledge graph builds (server mode, A8).

:func:`register` adds one periodic job, ``knowledge-graph-builds``, to the
worker's registration point (:mod:`spark.worker`), so only the active worker
builds. Every ``worker.knowledge_graph_poll_seconds`` it:

1. fails any running job with no progress for ``worker.knowledge_graph_stale_seconds``
   (its worker stopped mid-build), so the owner can retry it;
2. claims queued jobs one at a time (:func:`spark.knowledge.jobs.claim_next`)
   and builds each as the owner the job names.

Before a build the owner is resolved through warden at run time
(:class:`spark.scheduler.owners.WardenOwners`): an owner with no warden user,
or one not active, is refused, and so is a global graph build by an owner
without ``spark.knowledge.instance``. A refusal is recorded on the job and in
the audit trail. The build itself is the in-process engine
(:class:`~spark.knowledge.builder.KnowledgeGraphBuilder`) on a worker thread,
with every read and write scoped to the owner's guid, a model client from
:mod:`spark.llm.factory` over the instance settings as they are now, progress
written to the job, and the owner's cancel request checked before each chunk.
Its outcome (complete, failed or cancelled) is recorded on the job.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

from spark import worker
from spark.knowledge import jobs
from spark.knowledge.jobs import BuildJob

__all__ = [
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_STALE_SECONDS",
    "JOB_NAME",
    "ClientFactory",
    "ClientsFor",
    "GraphBuilds",
    "instance_clients",
    "register",
]

logger = logging.getLogger(__name__)

JOB_NAME: Final = "knowledge-graph-builds"
DEFAULT_POLL_SECONDS: Final = 5.0
DEFAULT_STALE_SECONDS: Final = 1800.0
DEFAULT_MAX_JOBS: Final = 20

# A model id to a client of its own for it (None when no provider lists it).
ClientFactory = Callable[[str], Any]
# The model clients for one run of the job, from the worker's context.
ClientsFor = Callable[[worker.WorkerContext], Awaitable[ClientFactory]]


class SettingsLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


async def instance_clients(ctx: worker.WorkerContext) -> ClientFactory:
    """Model clients from the instance settings as they are now, secrets from konfig."""
    from spark.instance_settings.accessor import ServerSettings
    from spark.instance_settings.consumers import InstanceView
    from spark.instance_settings.secrets import SecretsWriter
    from spark.llm.factory import ModelClients

    if ctx.app is None:
        raise worker.WorkerRefused("knowledge graph builds need the worker's application context")
    source = ServerSettings(ctx.database, ctx.app.settings)
    await source.reload()
    secrets = SecretsWriter(ctx.app.secrets, ctx.app.settings, server=True)
    return ModelClients(InstanceView(source, secrets))


def _embedder() -> Any:
    from spark.knowledge import tools

    return tools._get_embedder()


class _CancelToken:
    """The builder's cancel check, answered by the job row before each chunk.

    True when the owner cancelled (from any web replica), or when the job is no
    longer this worker's running job (failed as stale, say), so a job is never
    built twice at once.
    """

    def __init__(self, ctx: worker.WorkerContext, job_id: int) -> None:
        self._ctx = ctx
        self._job_id = job_id

    def is_cancelled(self) -> bool:
        return bool(self._ctx.sync_db.call(jobs.should_stop, self._job_id, self._ctx.worker_id))


def _refusal(job: BuildJob, permissions: frozenset[str] | None, refused: str | None) -> str | None:
    """Why the job's owner may not have this build now, or None."""
    from spark.identity.permissions import KNOWLEDGE_INSTANCE

    if refused is not None or permissions is None:
        return refused or "the build's owner cannot be resolved"
    if jobs.needs_instance_permission(job.scope) and KNOWLEDGE_INSTANCE not in permissions:
        return f"the build's owner may not build the global graph ({KNOWLEDGE_INSTANCE})"
    return None


class GraphBuilds:
    """The periodic job: claim queued builds and run each as its owner."""

    def __init__(
        self,
        *,
        clients: ClientsFor = instance_clients,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        max_jobs: int = DEFAULT_MAX_JOBS,
    ) -> None:
        self._clients = clients
        self._stale_seconds = stale_seconds
        self._max_jobs = max_jobs

    async def __call__(self, ctx: worker.WorkerContext) -> None:
        await self.run_once(ctx)

    async def run_once(self, ctx: worker.WorkerContext) -> list[int]:
        """Build up to ``max_jobs`` queued jobs; the ids of the jobs it finished."""
        from spark.scheduler.owners import WardenOwners

        if ctx.identity is None:
            # Checked before claiming, so the jobs stay queued for a worker that can.
            raise worker.WorkerRefused("knowledge graph builds need the worker's warden runtime")
        owners = WardenOwners(ctx.identity.identity, ctx.database)
        stale = await jobs.fail_stale(ctx.database, older_than_seconds=self._stale_seconds)
        if stale:
            logger.warning(
                "Failed %d knowledge graph build(s) left running by a stopped worker", stale
            )
        finished: list[int] = []
        factory: ClientFactory | None = None
        while len(finished) < self._max_jobs:
            job = await jobs.claim_next(ctx.database, ctx.worker_id)
            if job is None:
                break
            owner = await owners.resolve(job.user_guid)
            refusal = _refusal(job, owner.permissions, owner.refusal)
            if refusal is not None:
                await self._refuse(ctx, job, refusal)
            else:
                try:
                    if factory is None:
                        factory = await self._clients(ctx)
                    await self._build(ctx, job, factory)
                except Exception as exc:  # noqa: BLE001 - recorded on the job
                    await self._failed(ctx, job, exc)
            finished.append(job.id)
        return finished

    async def _refuse(self, ctx: worker.WorkerContext, job: BuildJob, reason: str) -> None:
        from spark.identity import audit

        logger.info("Knowledge graph build %d refused: %s", job.id, reason)
        await jobs.finish(
            ctx.database, job.id, ctx.worker_id, jobs.FAILED, error=f"Refused: {reason}"
        )
        await audit.emit(
            audit.KNOWLEDGE_BUILD_REFUSED,
            "failure",
            user_guid=job.user_guid,
            resource_type="knowledge_graph",
            resource_id=job.scope,
            details={"job_id": job.id, "reason": reason},
        )

    async def _failed(self, ctx: worker.WorkerContext, job: BuildJob, exc: Exception) -> None:
        logger.error("Knowledge graph build %d failed (%s)", job.id, type(exc).__name__)
        error = f"The build failed ({type(exc).__name__}: {exc})"[:500]
        await jobs.finish(
            ctx.database,
            job.id,
            ctx.worker_id,
            jobs.FAILED,
            result={"status": jobs.FAILED, "error": error},
            error=error,
        )

    async def _build(
        self, ctx: worker.WorkerContext, job: BuildJob, factory: ClientFactory
    ) -> None:
        from spark.knowledge.builder import KnowledgeGraphBuilder

        def progress(kind: str, data: dict[str, Any]) -> None:
            ctx.sync_db.call(jobs.record_progress, job.id, ctx.worker_id, data)

        engine = KnowledgeGraphBuilder(ctx.sync_db, factory, _embedder(), status_callback=progress)
        result: dict[str, Any] = await asyncio.to_thread(
            engine.build_scope,
            job.scope,
            job.user_guid,
            job.model_id,
            cancel_token=_CancelToken(ctx, job.id),
        )
        status = str(result.get("status"))
        outcome = status if status in jobs.OUTCOMES else jobs.FAILED
        error = result.get("error")
        await jobs.finish(
            ctx.database,
            job.id,
            ctx.worker_id,
            outcome,
            result=result,
            error=None if error is None else str(error),
        )


def _seconds(settings: SettingsLike, key: str, default: float) -> float:
    try:
        value = float(settings.get(key, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def register(settings: SettingsLike) -> None:
    """Register the builds on the worker (idempotent)."""
    if JOB_NAME in {job.name for job in worker.registered_periodic()}:
        return
    worker.register_periodic(
        JOB_NAME,
        _seconds(settings, "worker.knowledge_graph_poll_seconds", DEFAULT_POLL_SECONDS),
        GraphBuilds(
            stale_seconds=_seconds(
                settings, "worker.knowledge_graph_stale_seconds", DEFAULT_STALE_SECONDS
            )
        ),
    )
