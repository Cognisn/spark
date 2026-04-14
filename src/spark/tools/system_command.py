"""System command tool — execute shell commands with safety controls."""

from __future__ import annotations

import platform
import shlex
import subprocess
from pathlib import Path
from typing import Any

# Commands that are always blocked regardless of configuration.
_ALWAYS_BLOCKED: set[str] = {"mkfs", "fdisk", "dd", "format"}


def _get_platform_description() -> str:
    """Return an OS-aware description of the shell environment."""
    system = platform.system()
    if system == "Darwin":
        return "Execute a shell command on macOS using zsh."
    elif system == "Windows":
        return "Execute a shell command on Windows using cmd."
    else:
        return "Execute a shell command on Linux using bash."


def get_tools() -> list[dict[str, Any]]:
    """Return the tool definition for the run_command tool."""
    return [
        {
            "name": "run_command",
            "description": _get_platform_description(),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "Working directory for the command. Default: current directory.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Default: 30.",
                    },
                },
                "required": ["command"],
            },
        }
    ]


def execute(
    tool_name: str,
    tool_input: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> str:
    """Execute a system command tool.

    Parameters
    ----------
    tool_name:
        Must be ``"run_command"``.
    tool_input:
        Dictionary with ``command``, optional ``working_directory`` and ``timeout``.
    config:
        Optional configuration dictionary that may contain:
        - ``blocked_commands``: additional commands to block
        - ``max_timeout``: upper limit for timeout in seconds (default 120)
        - ``max_output_chars``: truncation limit for output (default 50000)
        - ``default_timeout``: default timeout when none specified (default 30)
    """
    if tool_name != "run_command":
        return f"Unknown tool: {tool_name}"

    if config is None:
        config = {}

    command = tool_input.get("command", "").strip()
    if not command:
        return "Error: no command provided."

    # --- Blocked command check ---
    blocked: set[str] = set(_ALWAYS_BLOCKED)
    extra_blocked = config.get("blocked_commands", [])
    if isinstance(extra_blocked, (list, set)):
        blocked.update(extra_blocked)

    # Extract the base command name (first token) for checking.
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if tokens:
        base_cmd = Path(tokens[0]).name.lower()
        if base_cmd in blocked:
            return f"Blocked: '{base_cmd}' is not permitted."

    # --- Working directory validation ---
    working_dir = tool_input.get("working_directory")
    if working_dir:
        wd_path = Path(working_dir)
        if not wd_path.is_dir():
            return f"Error: working directory does not exist: {working_dir}"
    else:
        working_dir = None

    # --- Timeout handling ---
    max_timeout = config.get("max_timeout", 120)
    default_timeout = config.get("default_timeout", 30)
    timeout = tool_input.get("timeout", default_timeout)
    timeout = min(int(timeout), int(max_timeout))

    # --- Output limit ---
    max_output_chars = config.get("max_output_chars", 50_000)

    # --- Determine shell for the current platform ---
    system = platform.system()

    try:
        if system == "Windows":
            # On Windows, use shell=True which delegates to cmd.exe.
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
        else:
            # On macOS use zsh; on Linux (and others) use bash.
            shell_executable = "/bin/zsh" if system == "Darwin" else "/bin/bash"
            result = subprocess.run(
                [shell_executable, "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout} seconds."
    except FileNotFoundError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: {exc}"

    # --- Build output ---
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr] {result.stderr}")
    if result.returncode != 0:
        parts.append(f"[exit code: {result.returncode}]")

    output = "\n".join(parts) if parts else "(no output)"

    # Truncate if necessary.
    if len(output) > max_output_chars:
        output = output[:max_output_chars] + "\n... (output truncated)"

    return output
