"""Tests for the system command tool."""

from __future__ import annotations

import platform
import tempfile
from pathlib import Path

import pytest

from spark.tools.system_command import execute, get_tools


class TestGetTools:
    """Verify get_tools returns the correct tool definition."""

    def test_returns_run_command(self) -> None:
        tools = get_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "run_command"

    def test_schema_has_required_params(self) -> None:
        schema = get_tools()[0]["inputSchema"]
        props = schema["properties"]
        assert "command" in props
        assert "working_directory" in props
        assert "timeout" in props
        assert schema["required"] == ["command"]

    def test_description_contains_platform(self) -> None:
        desc = get_tools()[0]["description"]
        system = platform.system()
        if system == "Darwin":
            assert "macOS" in desc
        elif system == "Windows":
            assert "Windows" in desc
        else:
            assert "Linux" in desc


class TestExecute:
    """Verify command execution behaviour."""

    def test_echo_returns_output(self) -> None:
        result = execute("run_command", {"command": "echo hello"})
        assert "hello" in result

    def test_stderr_is_captured(self) -> None:
        result = execute("run_command", {"command": "echo err >&2"})
        assert "[stderr]" in result
        assert "err" in result

    def test_blocked_always(self) -> None:
        result = execute("run_command", {"command": "dd if=/dev/zero of=/dev/null"})
        assert "Blocked" in result
        assert "dd" in result

    def test_blocked_from_config(self) -> None:
        config = {"blocked_commands": ["curl"]}
        result = execute("run_command", {"command": "curl https://example.com"}, config=config)
        assert "Blocked" in result
        assert "curl" in result

    def test_timeout(self) -> None:
        result = execute("run_command", {"command": "sleep 10", "timeout": 1})
        assert "timed out" in result

    def test_nonexistent_command(self) -> None:
        result = execute(
            "run_command",
            {"command": "this_command_surely_does_not_exist_xyz"},
        )
        # Should contain an error indication — either via stderr or exit code.
        assert "not found" in result.lower() or "exit code" in result.lower()

    def test_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute(
                "run_command",
                {"command": "pwd", "working_directory": tmpdir},
            )
            # The resolved real path should appear in the output.
            real_tmp = str(Path(tmpdir).resolve())
            assert real_tmp in result

    def test_invalid_working_directory(self) -> None:
        result = execute(
            "run_command",
            {"command": "echo hi", "working_directory": "/nonexistent_path_xyz"},
        )
        assert "does not exist" in result

    def test_unknown_tool_name(self) -> None:
        result = execute("unknown_tool", {"command": "echo hi"})
        assert "Unknown tool" in result

    def test_nonzero_exit_code(self) -> None:
        result = execute("run_command", {"command": "exit 42"})
        assert "[exit code: 42]" in result
