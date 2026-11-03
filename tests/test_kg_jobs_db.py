"""Knowledge graph build jobs (``spark.knowledge.jobs``) on SQLite and PostgreSQL.

The owner's functions are scoped: another user's job is never read, cancelled
or retried. One job per user and scope is active at a time. The worker's
functions claim the oldest queued job, record progress and the outcome only
while the job is still theirs, and fail a running job whose worker stopped
sending progress.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spark.knowledge import jobs

ADA = "ada-guid"
GRACE = "grace-guid"


async def _enqueue(db: Any, user: str = ADA, scope: str = "global") -> jobs.BuildJob:
    job = await jobs.enqueue(db, user, scope, "model-a")
    assert job is not None
    return job


class TestEnqueue:
    async def test_a_new_job_is_queued_for_its_owner(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        assert job.status == jobs.QUEUED
        assert (job.user_guid, job.scope, job.model_id) == (ADA, "global", "model-a")
        assert job.attempts == 0
        assert await jobs.get(spark_db, ADA, job.id) == job

    async def test_one_active_job_per_user_and_scope(self, spark_db: Any) -> None:
        await _enqueue(spark_db)
        assert await jobs.enqueue(spark_db, ADA, "global", "model-b") is None
        assert await jobs.enqueue(spark_db, ADA, "conv:1", "model-a") is not None
        assert await jobs.enqueue(spark_db, GRACE, "global", "model-a") is not None

    async def test_a_finished_job_does_not_block_the_next(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        claimed = await jobs.claim_next(spark_db, "w1")
        assert claimed is not None and claimed.id == job.id
        assert await jobs.finish(spark_db, job.id, "w1", jobs.COMPLETE, result={"chunks": 1})
        assert await jobs.enqueue(spark_db, ADA, "global", "model-a") is not None


class TestScoping:
    async def test_another_users_job_is_not_read(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db, ADA)
        assert await jobs.get(spark_db, GRACE, job.id) is None
        assert await jobs.active(spark_db, GRACE, "global") is None
        assert await jobs.latest(spark_db, GRACE, "global") is None

    async def test_another_users_job_is_not_cancelled(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db, ADA)
        assert await jobs.request_cancel(spark_db, GRACE, "global") == "not_building"
        found = await jobs.get(spark_db, ADA, job.id)
        assert found is not None and found.status == jobs.QUEUED

    async def test_another_users_job_is_not_retried(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db, ADA)
        await jobs.claim_next(spark_db, "w1")
        await jobs.finish(spark_db, job.id, "w1", jobs.FAILED, error="boom")
        outcome, retried = await jobs.retry(spark_db, GRACE, job.id)
        assert (outcome, retried) == ("not_found", None)
        found = await jobs.get(spark_db, ADA, job.id)
        assert found is not None and found.status == jobs.FAILED


class TestCancel:
    async def test_a_queued_job_is_cancelled_at_once(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        assert await jobs.request_cancel(spark_db, ADA, "global") == "cancelled"
        found = await jobs.get(spark_db, ADA, job.id)
        assert found is not None and found.status == jobs.CANCELLED
        assert found.finished_at is not None
        assert await jobs.claim_next(spark_db, "w1") is None

    async def test_a_running_job_is_asked_to_stop(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        await jobs.claim_next(spark_db, "w1")
        assert not await jobs.is_cancel_requested(spark_db, job.id)
        assert await jobs.request_cancel(spark_db, ADA, "global") == "cancelling"
        assert await jobs.is_cancel_requested(spark_db, job.id)
        found = await jobs.get(spark_db, ADA, job.id)
        assert found is not None and found.status == jobs.RUNNING

    async def test_nothing_to_cancel(self, spark_db: Any) -> None:
        assert await jobs.request_cancel(spark_db, ADA, "global") == "not_building"


class TestRetry:
    async def test_a_failed_job_is_queued_again(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        await jobs.claim_next(spark_db, "w1")
        await jobs.record_progress(spark_db, job.id, "w1", {"chunks": 2})
        await jobs.finish(spark_db, job.id, "w1", jobs.FAILED, error="provider down")

        outcome, retried = await jobs.retry(spark_db, ADA, job.id)

        assert outcome == "queued"
        assert retried is not None and retried.id == job.id
        assert retried.status == jobs.QUEUED
        assert (retried.error, retried.progress, retried.claimed_by) == (None, None, None)
        assert retried.attempts == 1
        again = await jobs.claim_next(spark_db, "w2")
        assert again is not None and again.id == job.id and again.attempts == 2

    async def test_only_a_failed_job_is_retried(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        assert (await jobs.retry(spark_db, ADA, job.id))[0] == "not_failed"
        await jobs.claim_next(spark_db, "w1")
        await jobs.finish(spark_db, job.id, "w1", jobs.COMPLETE, result={})
        assert (await jobs.retry(spark_db, ADA, job.id))[0] == "not_failed"

    async def test_a_retry_waits_for_the_active_job(self, spark_db: Any) -> None:
        failed = await _enqueue(spark_db)
        await jobs.claim_next(spark_db, "w1")
        await jobs.finish(spark_db, failed.id, "w1", jobs.FAILED, error="boom")
        await _enqueue(spark_db)
        assert (await jobs.retry(spark_db, ADA, failed.id))[0] == "busy"


class TestWorkerSide:
    async def test_claims_the_oldest_queued_job_once(self, spark_db: Any) -> None:
        first = await _enqueue(spark_db, ADA)
        second = await _enqueue(spark_db, GRACE)
        claimed = await jobs.claim_next(spark_db, "w1")
        assert claimed is not None
        assert (claimed.id, claimed.status, claimed.claimed_by) == (first.id, jobs.RUNNING, "w1")
        assert claimed.attempts == 1 and claimed.started_at is not None
        nxt = await jobs.claim_next(spark_db, "w2")
        assert nxt is not None and nxt.id == second.id
        assert await jobs.claim_next(spark_db, "w1") is None

    async def test_progress_and_outcome_only_from_the_claiming_worker(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        await jobs.claim_next(spark_db, "w1")
        assert not await jobs.record_progress(spark_db, job.id, "w2", {"chunks": 9})
        assert not await jobs.finish(spark_db, job.id, "w2", jobs.COMPLETE, result={})
        assert await jobs.record_progress(spark_db, job.id, "w1", {"chunks": 1})
        assert await jobs.finish(
            spark_db, job.id, "w1", jobs.COMPLETE, result={"status": "complete", "chunks": 1}
        )
        found = await jobs.get(spark_db, ADA, job.id)
        assert found is not None
        assert found.status == jobs.COMPLETE
        assert found.progress == {"chunks": 1}
        assert found.result == {"status": "complete", "chunks": 1}
        assert found.finished_at is not None
        assert not await jobs.record_progress(spark_db, job.id, "w1", {"chunks": 2})

    async def test_a_stale_running_job_is_failed_and_retryable(self, spark_db: Any) -> None:
        stale = await _enqueue(spark_db, ADA)
        fresh = await _enqueue(spark_db, GRACE)
        await jobs.claim_next(spark_db, "w1")
        await jobs.claim_next(spark_db, "w1")
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await spark_db.execute(
            "UPDATE kg_build_jobs SET updated_at = ? WHERE id = ?", (old, stale.id)
        )

        assert await jobs.fail_stale(spark_db, older_than_seconds=600) == 1

        found = await jobs.get(spark_db, ADA, stale.id)
        assert found is not None and found.status == jobs.FAILED and found.error
        other = await jobs.get(spark_db, GRACE, fresh.id)
        assert other is not None and other.status == jobs.RUNNING
        assert (await jobs.retry(spark_db, ADA, stale.id))[0] == "queued"

    async def test_a_build_stops_once_the_job_is_not_its_running_job(self, spark_db: Any) -> None:
        job = await _enqueue(spark_db)
        await jobs.claim_next(spark_db, "w1")
        assert not await jobs.should_stop(spark_db, job.id, "w1")
        assert await jobs.should_stop(spark_db, job.id, "w2")
        await jobs.request_cancel(spark_db, ADA, "global")
        assert await jobs.should_stop(spark_db, job.id, "w1")

        other = await _enqueue(spark_db, GRACE)
        await jobs.claim_next(spark_db, "w1")
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await spark_db.execute(
            "UPDATE kg_build_jobs SET updated_at = ? WHERE id = ?", (old, other.id)
        )
        await jobs.fail_stale(spark_db, older_than_seconds=600)
        assert await jobs.should_stop(spark_db, other.id, "w1")
        assert await jobs.should_stop(spark_db, 999999, "w1")

    async def test_latest_and_latest_finished(self, spark_db: Any) -> None:
        done = await _enqueue(spark_db)
        await jobs.claim_next(spark_db, "w1")
        await jobs.finish(spark_db, done.id, "w1", jobs.COMPLETE, result={"chunks": 3})
        queued = await _enqueue(spark_db)
        latest = await jobs.latest(spark_db, ADA, "global")
        finished = await jobs.latest_finished(spark_db, ADA, "global")
        assert latest is not None and latest.id == queued.id
        assert finished is not None and finished.id == done.id
        active = await jobs.active(spark_db, ADA, "global")
        assert active is not None and active.id == queued.id


@pytest.mark.parametrize("status", [jobs.RUNNING, jobs.QUEUED, "bogus"])
async def test_finish_refuses_a_status_that_is_not_an_outcome(spark_db: Any, status: str) -> None:
    job = await _enqueue(spark_db)
    await jobs.claim_next(spark_db, "w1")
    with pytest.raises(ValueError):
        await jobs.finish(spark_db, job.id, "w1", status, result={})


def test_public_dict_has_no_owner_guid() -> None:
    job = jobs.BuildJob(
        id=1,
        user_guid=ADA,
        scope="global",
        model_id="m",
        status=jobs.QUEUED,
        attempts=0,
        cancel_requested=False,
        claimed_by="w1",
        progress=None,
        result=None,
        error=None,
        created_at="t",
        updated_at="t",
        started_at=None,
        finished_at=None,
    )
    public = job.public()
    assert "user_guid" not in public and "claimed_by" not in public
    assert public["id"] == 1 and public["status"] == jobs.QUEUED
