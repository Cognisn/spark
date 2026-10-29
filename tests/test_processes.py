"""pid_alive must probe a process without signalling it (Windows: signal 0 is Ctrl+C)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from spark.core import processes
from spark.core.processes import pid_alive


def test_current_process_is_alive():
    assert pid_alive(os.getpid()) is True


def test_finished_process_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert pid_alive(child.pid) is False


@pytest.mark.parametrize("pid", [0, -1])
def test_invalid_pids_are_not_alive(pid):
    assert pid_alive(pid) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_probe_never_calls_os_kill(monkeypatch):
    """os.kill(pid, 0) sends CTRL_C_EVENT to the console group on Windows."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"os.kill called with {args}")

    monkeypatch.setattr(processes.os, "kill", forbidden)
    assert pid_alive(os.getpid()) is True


def test_daemon_manager_uses_the_safe_probe(monkeypatch):
    from spark.daemon.manager import DaemonManager

    calls: list[int] = []
    monkeypatch.setattr("spark.daemon.manager.pid_alive", lambda pid: calls.append(pid) or True)
    assert DaemonManager._is_process_alive(4242) is True
    assert calls == [4242]
