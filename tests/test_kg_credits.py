"""Knowledge graph builds: metered for their user, and stopped at 100% of their credits (M6).

Each chunk is a new request on the build model's connection: a build already past
the user's allowance extracts nothing, and one that reaches it part-way keeps what
it has and stops before the next chunk. The model is conduit's stub provider through
the real connection runtime, so every usage row comes from conduit's events.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from spark.database import conversations
from spark.knowledge.builder import KnowledgeGraphBuilder
from tests.credit_helpers import allow, metered, server_mode, spent  # noqa: F401
from tests.model_helpers import ScriptedModels
from tests.test_kg_extractor import tool_response

USER = "u1"
MODEL = "model-kg"


class FakeEmbedder:
    def encode(self, text: str, normalize: bool = True) -> Any:
        return np.ones(8, dtype=np.float32)


def _fragment(name: str) -> dict[str, Any]:
    # 10 input and 5 output tokens: 15 credits at tokens-as-credits rates.
    return tool_response([{"name": name, "entity_type": "thing", "description": "x"}], [])


@pytest.fixture()
def models(sync_db: Any) -> Iterator[ScriptedModels]:
    scripted = ScriptedModels(sync_db, {MODEL: [_fragment("Alpha"), _fragment("Beta")]})
    yield scripted
    scripted.close()


def _two_chunks(db: Any) -> str:
    from spark.database import messages as msg_db

    cid = db.call(conversations.create_conversation, "c", MODEL, USER)
    for word in ("alpha", "beta"):
        db.call(msg_db.add_message, cid, "user", word * 1400, 10, USER)  # 7,000 characters
    return f"conv:{cid}"


def _build(db: Any, models: ScriptedModels, scope: str, roles: Any = None) -> dict[str, Any]:
    builder = KnowledgeGraphBuilder(db, models.factory, FakeEmbedder())
    result: dict[str, Any] = builder.build_scope(scope, USER, MODEL, roles=roles)
    return result


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all("SELECT user_guid, source, connection_id FROM usage_tracking ORDER BY id")


class TestKnowledgeBuilds:
    def test_every_call_is_billed_to_the_user_as_knowledge(
        self, sync_db: Any, models: ScriptedModels
    ) -> None:
        result = _build(sync_db, models, _two_chunks(sync_db))
        assert (result["status"], result["chunks"]) == ("complete", 2)
        assert (
            _rows(sync_db)
            == [{"user_guid": USER, "source": "knowledge", "connection_id": "stub"}] * 2
        )

    def test_past_the_allowance_nothing_is_extracted(
        self, sync_db: Any, models: ScriptedModels, server_mode: None
    ) -> None:
        sync_db.call(metered, name="Work Bedrock")
        sync_db.call(allow, role="user", credits=10)
        sync_db.call(spent, USER, 10)
        result = _build(sync_db, models, _two_chunks(sync_db), roles={"user"})
        assert (result["status"], result["chunks"]) == ("failed", 0)
        assert "monthly credits on Work Bedrock" in result["error"]
        assert models.calls[MODEL] == 0

    def test_a_chunk_finishes_then_the_build_stops(
        self, sync_db: Any, models: ScriptedModels, server_mode: None
    ) -> None:
        sync_db.call(metered)
        sync_db.call(allow, role="user", credits=15)
        result = _build(sync_db, models, _two_chunks(sync_db), roles={"user"})
        assert (result["status"], result["chunks"], result["entities"]) == ("failed", 1, 1)
        assert models.calls[MODEL] == 1

    def test_unmetered_builds_never_stop(
        self, sync_db: Any, models: ScriptedModels, server_mode: None
    ) -> None:
        sync_db.call(metered, is_metered=False)
        sync_db.call(allow, role="user", credits=1)
        sync_db.call(spent, USER, 10**6)
        result = _build(sync_db, models, _two_chunks(sync_db), roles={"user"})
        assert (result["status"], result["chunks"]) == ("complete", 2)


class TestWorkerPassesTheOwnersRoles:
    async def test_the_owners_roles_reach_the_build(self, monkeypatch: Any) -> None:
        from spark.knowledge import build_jobs
        from spark.knowledge.jobs import BuildJob

        seen: dict[str, Any] = {}

        class Engine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def build_scope(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                seen.update(kwargs)
                return {"status": "complete"}

        async def finish(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr("spark.knowledge.builder.KnowledgeGraphBuilder", Engine)
        monkeypatch.setattr(build_jobs.jobs, "finish", finish)
        monkeypatch.setattr(build_jobs, "_embedder", lambda: None)

        class Ctx:
            sync_db = None
            database = None
            worker_id = "w1"

        job = BuildJob(
            id=1,
            user_guid=USER,
            scope="conv:1",
            model_id=MODEL,
            status="running",
            attempts=1,
            cancel_requested=False,
            claimed_by="w1",
            progress=None,
            result=None,
            error=None,
            created_at="",
            updated_at="",
            started_at=None,
            finished_at=None,
        )
        builds = build_jobs.GraphBuilds()
        await builds._build(Ctx(), job, lambda m: None, frozenset({"user"}))  # type: ignore[arg-type]
        assert seen["roles"] == frozenset({"user"})
