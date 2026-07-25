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


class TestCancellation:
    """Tests for cooperative cancellation in AgentExecutor.execute()."""

    def _make_response(self, content: str = "ok") -> dict:
        return {
            "content": content,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": content}],
        }

    def test_cancel_before_first_iteration_returns_cancelled(self) -> None:
        from spark.core.cancellation import CancellationToken

        llm = MagicMock()
        # invoke_model should never be called once we cancel up-front.
        llm.invoke_model.side_effect = AssertionError("LLM was invoked after cancel")

        events: list[tuple[str, dict]] = []

        def status(event_type: str, data: dict) -> None:
            events.append((event_type, data))

        ex = AgentExecutor(
            llm,
            MagicMock(),
            {"embedded_tools": {}},
            user_guid="u",
            status_callback=status,
        )
        token = CancellationToken()
        token.cancel("user")

        result = ex.execute(
            "agent-1",
            "TestAgent",
            "do work",
            "stub-model",
            cancel_token=token,
        )

        assert result["status"] == "cancelled"
        assert "[CANCELLED]" in result["content"]
        completes = [d for et, d in events if et == "agent_complete"]
        assert completes, "expected an agent_complete status event"
        assert completes[-1]["status"] == "cancelled"

    def test_no_token_runs_to_completion(self) -> None:
        llm = MagicMock()
        llm.invoke_model.return_value = self._make_response("done")
        ex = AgentExecutor(llm, MagicMock(), {"embedded_tools": {}}, user_guid="u")
        result = ex.execute("a", "Agent", "task", "stub-model")
        assert result.get("status", "completed") == "completed"
        assert result["content"] == "done"

    def test_cancel_between_iterations(self) -> None:
        from spark.core.cancellation import CancellationToken

        token = CancellationToken()

        # First call returns tool_use (forcing another iteration); cancel after.
        responses = [
            {
                "content": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "tool_use": [{"type": "tool_use", "id": "t1", "name": "noop", "input": {}}],
                "content_blocks": [{"type": "tool_use", "id": "t1", "name": "noop", "input": {}}],
            },
            self._make_response("should not be reached"),
        ]
        call_index = {"i": 0}

        def fake_invoke(*args, **kwargs):
            i = call_index["i"]
            call_index["i"] += 1
            return responses[i]

        llm = MagicMock()
        llm.invoke_model.side_effect = fake_invoke
        ex = AgentExecutor(llm, MagicMock(), {"embedded_tools": {}}, user_guid="u")

        # Patch _execute_tool to avoid touching the real tool registry, and to
        # cancel after the first tool runs.
        def fake_tool(name: str, inp: dict) -> str:
            token.cancel("user")
            return "tool result"

        ex._execute_tool = fake_tool  # type: ignore[method-assign]

        result = ex.execute(
            "agent-2",
            "TestAgent",
            "do work",
            "stub-model",
            cancel_token=token,
        )

        assert result["status"] == "cancelled"
        # The second invoke must not have been reached.
        assert call_index["i"] == 1


class TestExecutorExtensions:
    """Tests for system_override, exclude_tools, extra_tools, terminal_tool."""

    def _end_turn(self, content: str = "done") -> dict:
        return {
            "content": content,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": content}],
        }

    def _tool_use(self, name: str, tool_input: dict) -> dict:
        return {
            "content": "",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "tool_use": [{"id": "t1", "name": name, "input": tool_input}],
            "content_blocks": [],
        }

    def test_system_override_used(self, executor: AgentExecutor) -> None:
        executor._llm.invoke_model.return_value = self._end_turn()
        executor.execute("a1", "n", "task", "m", system_override="CUSTOM SYSTEM")
        assert executor._llm.invoke_model.call_args.kwargs["system"] == "CUSTOM SYSTEM"

    def test_extra_tools_offered_and_excluded_removed(self, executor: AgentExecutor) -> None:
        extra = {"name": "submit_argument", "description": "d", "inputSchema": {"type": "object"}}
        executor._llm.invoke_model.return_value = self._end_turn()
        executor.execute(
            "a1",
            "n",
            "task",
            "m",
            extra_tools=[extra],
            exclude_tools={"store_memory"},
        )
        tools = executor._llm.invoke_model.call_args.kwargs["tools"]
        names = {t["name"] for t in tools}
        assert "submit_argument" in names and "store_memory" not in names

    def test_terminal_tool_returns_capture_without_executing(self, executor: AgentExecutor) -> None:
        executor._llm.invoke_model.return_value = self._tool_use(
            "submit_argument", {"argument_markdown": "case", "exhibits": []}
        )
        result = executor.execute(
            "a1",
            "n",
            "task",
            "m",
            extra_tools=[
                {"name": "submit_argument", "description": "d", "inputSchema": {"type": "object"}}
            ],
            terminal_tool="submit_argument",
        )
        assert result["status"] == "completed"
        assert result["terminal_call"]["input"]["argument_markdown"] == "case"
        assert executor._llm.invoke_model.call_count == 1

    def test_excluded_tool_refused_at_execution(self, executor: AgentExecutor) -> None:
        executor._llm.invoke_model.side_effect = [
            self._tool_use("store_memory", {"content": "x"}),
            self._end_turn(),
        ]
        executor.execute("a1", "n", "task", "m", exclude_tools={"store_memory"})
        # The second call's messages must contain a refusal tool result
        messages = executor._llm.invoke_model.call_args.args[0]
        refusals = [
            item
            for m in messages
            if isinstance(m.get("content"), list)
            for item in m["content"]
            if isinstance(item, dict) and "not available" in str(item.get("content", ""))
        ]
        assert refusals
