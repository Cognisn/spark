"""Server mode: the knowledge endpoint queues builds as jobs and reads status from them.

The real app on a PostgreSQL clone at head, with synthetic users signed in
through warden (the fixtures of ``tests.test_user_scoping_web`` and
``tests.test_models_offered_web``). No build runs in the web process: a build
request queues a job for the worker, and the status page of any web replica
reads the job back. The global graph needs ``spark.knowledge.instance``;
another user's job is never read, cancelled or retried.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations
from spark.database import messages as msg_db
from spark.identity.permissions import POWER_USER, USER
from spark.knowledge import jobs
from spark.mode import DatabaseSettings
from tests.identity_web_helpers import make_user, server_client, sign_in, write_config
from tests.test_models_offered_web import OllamaStandIn, shared  # noqa: F401 - fixtures
from tests.test_user_scoping_web import (  # noqa: F401 - fixtures
    FETCH,
    _call,
    _signed_in,
    client,
    host,
    server_db,
)

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("shared")]

ADA = "ada.kg@example.com"
GRACE = "grace.kg@example.com"


@pytest.fixture(autouse=True)
def no_build_in_the_web_process(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server's web process must never make a model client for a build."""
    from spark.knowledge.builder import KnowledgeGraphBuilder

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a knowledge graph build ran in the web process")

    monkeypatch.setattr(KnowledgeGraphBuilder, "build_scope", refuse)


def _conversation(client: TestClient, guid: str) -> int:
    cid: int = _call(client, conversations.create_conversation, "c", "model-a", guid)
    _call(client, msg_db.add_message, cid, "user", "Tea is a drink", 5, guid)
    return cid


def _claim_and_finish(client: TestClient, status: str, **outcome: Any) -> jobs.BuildJob:
    """What the worker does with the oldest queued job."""
    job = _call(client, jobs.claim_next, "worker-test")
    assert job is not None
    assert _call(client, jobs.finish, job.id, "worker-test", status, **outcome)
    found: jobs.BuildJob = _call(client, jobs.get, job.user_guid, job.id)
    return found


class TestBuildQueuesAJob:
    def test_a_build_is_queued_not_run(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        cid = _conversation(client, guid)

        started = ada.post("/knowledge/api/build", json={"scope": f"conv:{cid}"})

        assert started.status_code == 200, started.text
        body = started.json()
        assert body["status"] == "queued" and body["scope"] == f"conv:{cid}"
        job = _call(client, jobs.get, guid, body["job_id"])
        assert job is not None and job.status == jobs.QUEUED
        assert job.model_id in ("model-a", "model-b")

    def test_a_second_build_of_the_scope_waits(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        assert ada.post("/knowledge/api/build", json={"scope": "global"}).status_code == 200
        again = ada.post("/knowledge/api/build", json={"scope": "global"})
        assert again.status_code == 409

    def test_another_users_conversation_is_not_found(self, client: TestClient) -> None:
        _, ada_guid = _signed_in(client, ADA, POWER_USER)
        grace, grace_guid = _signed_in(client, GRACE, POWER_USER)
        cid = _conversation(client, ada_guid)
        refused = grace.post("/knowledge/api/build", json={"scope": f"conv:{cid}"})
        assert refused.status_code == 404
        assert _call(client, jobs.latest, grace_guid, f"conv:{cid}") is None


class TestInstancePermission:
    def test_the_global_graph_needs_the_instance_permission(self, client: TestClient) -> None:
        grace, guid = _signed_in(client, GRACE, USER)
        cid = _conversation(client, guid)

        refused = grace.post("/knowledge/api/build", json={"scope": "global"})
        assert refused.status_code == 403
        assert _call(client, jobs.latest, guid, "global") is None
        assert grace.post("/knowledge/api/clear", json={"scope": "global"}).status_code == 403

        allowed = grace.post("/knowledge/api/build", json={"scope": f"conv:{cid}"})
        assert allowed.status_code == 200, allowed.text

    def test_a_power_user_builds_the_global_graph(self, client: TestClient) -> None:
        ada, _ = _signed_in(client, ADA, POWER_USER)
        assert ada.post("/knowledge/api/build", json={"scope": "global"}).status_code == 200


class TestStatusFromTheJob:
    def test_status_follows_the_job(self, client: TestClient) -> None:
        ada, _ = _signed_in(client, ADA, POWER_USER)
        ada.post("/knowledge/api/build", json={"scope": "global"})

        queued = ada.get("/knowledge/api/status").json()["scopes"]["global"]
        assert queued["building"] is True
        assert queued["job"]["status"] == jobs.QUEUED
        assert "user_guid" not in queued["job"]

        done = _claim_and_finish(
            client, jobs.COMPLETE, result={"status": "complete", "chunks": 1, "entities": 2}
        )
        finished = ada.get("/knowledge/api/status").json()["scopes"]["global"]
        assert finished["building"] is False
        assert finished["job"]["id"] == done.id
        assert finished["last_result"]["status"] == "complete"
        assert finished["last_result"]["entities"] == 2

    def test_any_replica_reads_the_job(
        self, client: TestClient, server_db: DatabaseSettings, tmp_path: Path
    ) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        started = ada.post("/knowledge/api/build", json={"scope": "global"}).json()

        config = write_config(tmp_path / "replica.yaml")
        with server_client(config, server_db) as replica:
            make_user_ok = sign_in(replica, ADA)
            assert make_user_ok.status_code == 303, make_user_ok.text
            seen = replica.get("/knowledge/api/status").json()["scopes"]["global"]
            assert seen["building"] is True
            assert seen["job"]["id"] == started["job_id"]

            _claim_and_finish(client, jobs.FAILED, error="provider down")
            failed = replica.get("/knowledge/api/status").json()["scopes"]["global"]
            assert failed["building"] is False
            assert failed["job"]["status"] == jobs.FAILED
            assert failed["job"]["error"] == "provider down"

            retried = replica.post("/knowledge/api/retry", json={"job_id": started["job_id"]})
            assert retried.status_code == 200, retried.text
            assert retried.json()["status"] == "queued"
        job = _call(client, jobs.get, guid, started["job_id"])
        assert job is not None and job.status == jobs.QUEUED


class TestRetryAndCancel:
    def test_retry_is_scoped_to_the_owner(self, client: TestClient) -> None:
        ada, _ = _signed_in(client, ADA, POWER_USER)
        grace, _ = _signed_in(client, GRACE, POWER_USER)
        job_id = ada.post("/knowledge/api/build", json={"scope": "global"}).json()["job_id"]
        _claim_and_finish(client, jobs.FAILED, error="boom")

        assert grace.post("/knowledge/api/retry", json={"job_id": job_id}).status_code == 404
        assert ada.post("/knowledge/api/retry", json={"job_id": job_id}).status_code == 200

    def test_only_a_failed_job_is_retried(self, client: TestClient) -> None:
        ada, _ = _signed_in(client, ADA, POWER_USER)
        job_id = ada.post("/knowledge/api/build", json={"scope": "global"}).json()["job_id"]
        assert ada.post("/knowledge/api/retry", json={"job_id": job_id}).status_code == 409
        assert ada.post("/knowledge/api/retry", json={"job_id": "x"}).status_code == 400
        assert ada.post("/knowledge/api/retry", json={}).status_code == 400

    def test_a_retry_of_the_global_graph_needs_the_permission(self, client: TestClient) -> None:
        grace, guid = _signed_in(client, GRACE, USER)
        job = _call(client, jobs.enqueue, guid, "global", "model-a")
        _claim_and_finish(client, jobs.FAILED, error="Refused")
        assert grace.post("/knowledge/api/retry", json={"job_id": job.id}).status_code == 403

    def test_cancel_a_queued_job(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        ada.post("/knowledge/api/build", json={"scope": "global"})
        grace, _ = _signed_in(client, GRACE, POWER_USER)
        assert grace.post("/knowledge/api/cancel", json={"scope": "global"}).json() == {
            "status": "not_building"
        }

        cancelled = ada.post("/knowledge/api/cancel", json={"scope": "global"})

        assert cancelled.json() == {"status": "cancelled"}
        latest = _call(client, jobs.latest, guid, "global")
        assert latest is not None and latest.status == jobs.CANCELLED

    def test_cancel_a_running_job(self, client: TestClient) -> None:
        ada, guid = _signed_in(client, ADA, POWER_USER)
        ada.post("/knowledge/api/build", json={"scope": "global"})
        claimed = _call(client, jobs.claim_next, "worker-test")
        assert ada.post("/knowledge/api/cancel", json={"scope": "global"}).json() == {
            "status": "cancelling"
        }
        assert _call(client, jobs.is_cancel_requested, claimed.id)

    def test_clear_waits_for_an_active_job(self, client: TestClient) -> None:
        ada, _ = _signed_in(client, ADA, POWER_USER)
        ada.post("/knowledge/api/build", json={"scope": "global"})
        assert ada.post("/knowledge/api/clear", json={"scope": "global"}).status_code == 409
        _claim_and_finish(client, jobs.COMPLETE, result={})
        assert ada.post("/knowledge/api/clear", json={"scope": "global"}).status_code == 200
