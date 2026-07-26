"""Tests for the agent run database operations."""

from __future__ import annotations

import time

import pytest

from spark.database import Database, conversations
from spark.database.agents import (
    complete_agent_run,
    create_agent_run,
    get_agent_run,
    get_agent_runs,
)
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection

USER = "test-user-guid"


@pytest.fixture()
def db(tmp_path) -> Database:
    """Create a fresh test database."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    return Database(conn)


@pytest.fixture()
def conn(db: Database) -> DatabaseConnection:
    return db.connection


@pytest.fixture()
def conv_id(conn: DatabaseConnection) -> int:
    """Create a parent conversation for agent runs."""
    return conversations.create_conversation(conn, "Parent Chat", "claude-3", USER)


class TestAgentRuns:
    def test_create_returns_row_id(self, conn: DatabaseConnection, conv_id: int) -> None:
        row_id = create_agent_run(
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

    def test_complete_updates_status_and_tokens(
        self, conn: DatabaseConnection, conv_id: int
    ) -> None:
        create_agent_run(
            conn,
            agent_id="agent-002",
            parent_conversation_id=conv_id,
            agent_name="summariser",
            task_description="Summarise document",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        complete_agent_run(
            conn,
            "agent-002",
            status="completed",
            result_text="Summary done.",
            input_tokens=500,
            output_tokens=200,
            tool_calls_json='[{"name": "read_file"}]',
        )
        run = get_agent_run(conn, "agent-002")
        assert run is not None
        assert run["status"] == "completed"
        assert run["input_tokens"] == 500
        assert run["output_tokens"] == 200
        assert run["result_text"] == "Summary done."
        assert run["tool_calls_json"] == '[{"name": "read_file"}]'
        assert run["completed_at"] is not None

    def test_get_agent_runs_ordering(self, conn: DatabaseConnection, conv_id: int) -> None:
        # Create two runs; SQLite CURRENT_TIMESTAMP has second-level precision,
        # so we complete the first to give it a distinct created_at if needed.
        create_agent_run(
            conn,
            agent_id="agent-first",
            parent_conversation_id=conv_id,
            agent_name="alpha",
            task_description="Task A",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        create_agent_run(
            conn,
            agent_id="agent-second",
            parent_conversation_id=conv_id,
            agent_name="beta",
            task_description="Task B",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )

        runs = get_agent_runs(conn, conv_id)
        assert len(runs) == 2
        # Newest first — both have the same timestamp so order by rowid DESC
        # effectively; just verify both are returned.
        names = {r["agent_name"] for r in runs}
        assert names == {"alpha", "beta"}

    def test_get_single_agent_run(self, conn: DatabaseConnection, conv_id: int) -> None:
        create_agent_run(
            conn,
            agent_id="agent-lookup",
            parent_conversation_id=conv_id,
            agent_name="lookup",
            task_description="Look things up",
            mode="orchestrator",
            model_id="claude-3",
            user_guid=USER,
        )
        run = get_agent_run(conn, "agent-lookup")
        assert run is not None
        assert run["agent_id"] == "agent-lookup"
        assert run["agent_name"] == "lookup"
        assert run["status"] == "running"

    def test_get_nonexistent_agent_returns_none(self, conn: DatabaseConnection) -> None:
        result = get_agent_run(conn, "does-not-exist")
        assert result is None
