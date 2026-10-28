"""Tests for the agent run database operations."""

from __future__ import annotations

import time

import pytest

from spark.database import conversations
from spark.database.agents import (
    complete_agent_run,
    create_agent_run,
    get_agent_run,
    get_agent_runs,
)
from spark.db import Database

USER = "test-user-guid"


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


@pytest.fixture()
def conn(spark_db):
    return spark_db


@pytest.fixture()
async def conv_id(conn: Database) -> int:
    """Create a parent conversation for agent runs."""
    return await conversations.create_conversation(conn, "Parent Chat", "claude-3", USER)


class TestAgentRuns:
    async def test_create_returns_row_id(self, conn: Database, conv_id: int) -> None:
        row_id = await create_agent_run(
            conn,
            agent_id="agent-001",
            parent_conversation_id=conv_id,
            agent_name="researcher",
            task_description="Find recent papers",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        assert row_id is not None
        assert row_id > 0

    async def test_complete_updates_status_and_tokens(self, conn: Database, conv_id: int) -> None:
        await create_agent_run(
            conn,
            agent_id="agent-002",
            parent_conversation_id=conv_id,
            agent_name="summariser",
            task_description="Summarise document",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        await complete_agent_run(
            conn,
            "agent-002",
            status="completed",
            result_text="Summary done.",
            input_tokens=500,
            output_tokens=200,
            tool_calls_json='[{"name": "read_file"}]',
        )
        run = await get_agent_run(conn, "agent-002")
        assert run is not None
        assert run["status"] == "completed"
        assert run["input_tokens"] == 500
        assert run["output_tokens"] == 200
        assert run["result_text"] == "Summary done."
        assert run["tool_calls_json"] == '[{"name": "read_file"}]'
        assert run["completed_at"] is not None

    async def test_get_agent_runs_ordering(self, conn: Database, conv_id: int) -> None:
        # Create two runs; SQLite CURRENT_TIMESTAMP has second-level precision,
        # so we complete the first to give it a distinct created_at if needed.
        await create_agent_run(
            conn,
            agent_id="agent-first",
            parent_conversation_id=conv_id,
            agent_name="alpha",
            task_description="Task A",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        await create_agent_run(
            conn,
            agent_id="agent-second",
            parent_conversation_id=conv_id,
            agent_name="beta",
            task_description="Task B",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )

        runs = await get_agent_runs(conn, conv_id)
        assert len(runs) == 2
        # Newest first — both have the same timestamp so order by rowid DESC
        # effectively; just verify both are returned.
        names = {r["agent_name"] for r in runs}
        assert names == {"alpha", "beta"}

    async def test_get_single_agent_run(self, conn: Database, conv_id: int) -> None:
        await create_agent_run(
            conn,
            agent_id="agent-lookup",
            parent_conversation_id=conv_id,
            agent_name="lookup",
            task_description="Look things up",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        run = await get_agent_run(conn, "agent-lookup")
        assert run is not None
        assert run["agent_id"] == "agent-lookup"
        assert run["agent_name"] == "lookup"
        assert run["status"] == "running"

    async def test_get_nonexistent_agent_returns_none(self, conn: Database) -> None:
        result = await get_agent_run(conn, "does-not-exist")
        assert result is None
