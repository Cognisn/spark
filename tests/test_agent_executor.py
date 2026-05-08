"""Tests for the AgentExecutor sub-agent tool-use loop."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spark.core.agent_executor import AgentExecutor


@pytest.fixture()
def executor() -> AgentExecutor:
    """Create an AgentExecutor with mocked dependencies."""
    llm = MagicMock()
    db = MagicMock()
    config: dict = {"embedded_tools": {}}
    return AgentExecutor(llm, db, config, user_guid="test-user")


class TestBuildSystem:
    """Tests for AgentExecutor._build_system()."""

    def test_contains_agent_name(self, executor: AgentExecutor) -> None:
        result = executor._build_system("ResearchBot", "Find recent papers", "orchestrator")
        assert "ResearchBot" in result

    def test_contains_task(self, executor: AgentExecutor) -> None:
        task = "Summarise the quarterly report"
        result = executor._build_system("SummaryAgent", task, "orchestrator")
        assert task in result

    def test_contains_os_info(self, executor: AgentExecutor) -> None:
        result = executor._build_system("TestAgent", "do something", "orchestrator")
        # Should contain one of the recognised OS strings
        assert any(keyword in result for keyword in ("macOS", "Windows", "Linux"))

    def test_contains_utc_timestamp(self, executor: AgentExecutor) -> None:
        result = executor._build_system("TestAgent", "do something", "orchestrator")
        assert "UTC" in result

    def test_is_string(self, executor: AgentExecutor) -> None:
        result = executor._build_system("Agent", "task", "orchestrator")
        assert isinstance(result, str)


class TestGetTools:
    """Tests for AgentExecutor._get_tools()."""

    def test_returns_list(self, executor: AgentExecutor) -> None:
        # Without real tool registry wired up, _get_tools should still
        # return a list (possibly empty if the import fails gracefully).
        result = executor._get_tools()
        assert isinstance(result, list)
