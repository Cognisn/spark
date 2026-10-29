"""Tests for the system command tool."""

from __future__ import annotations

import os
import platform
import sys
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
            # Ask Python for the working directory: `pwd` prints a POSIX-style
            # path under the Windows shells, so compare normalised real paths.
            command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'
            result = execute(
                "run_command",
                {"command": command, "working_directory": tmpdir},
            )
            real_tmp = os.path.normcase(str(Path(tmpdir).resolve()))
            printed = [
                os.path.normcase(str(Path(line.strip()).resolve()))
                for line in result.splitlines()
                if line.strip()
            ]
            assert real_tmp in printed

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


class TestChainedBlocklist:
    """The blocklist applies to every command in a chain before anything runs."""

    @pytest.fixture
    def no_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def _refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("a blocked command line reached the shell")

        monkeypatch.setattr(subprocess, "run", _refuse)

    @pytest.mark.parametrize(
        ("system", "command"),
        [
            ("Linux", "ls; curl https://example.com"),
            ("Linux", "ls && curl https://example.com"),
            ("Linux", "echo $(curl https://example.com)"),
            ("Darwin", "ls | curl https://example.com"),
            ("Darwin", "echo `curl https://example.com`"),
            ("Windows", "dir & curl https://example.com"),
            ("Windows", 'cmd /c "dir && curl https://example.com"'),
            ("Windows", 'powershell -Command "Get-Date; curl https://example.com"'),
        ],
    )
    def test_chained_command_is_blocked(
        self, no_shell: None, monkeypatch: pytest.MonkeyPatch, system: str, command: str
    ) -> None:
        monkeypatch.setattr(platform, "system", lambda: system)
        result = execute("run_command", {"command": command}, config={"blocked_commands": ["curl"]})
        assert result == "Blocked: 'curl' is not permitted."

    def test_always_blocked_inside_a_chain(self, no_shell: None) -> None:
        result = execute("run_command", {"command": "echo start; mkfs /dev/null"})
        assert result == "Blocked: 'mkfs' is not permitted."

    def test_unparseable_command_is_refused(self, no_shell: None) -> None:
        result = execute("run_command", {"command": 'echo "unterminated'})
        assert result.startswith("Refused: the command could not be checked safely")

    def test_allowed_chain_still_runs(self) -> None:
        result = execute("run_command", {"command": "echo one && echo two"})
        assert "one" in result
        assert "two" in result
