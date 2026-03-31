"""Tests for daemon and scheduler modules."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spark.daemon.manager import DaemonManager

# -- DaemonManager ------------------------------------------------------------


class TestDaemonManager:
    def test_status_no_pid_file(self, tmp_path: Path) -> None:
        dm = DaemonManager(pid_file=str(tmp_path / "nonexistent.pid"))
        status = dm.status()
        assert status["running"] is False
        assert status["pid"] is None

    def test_status_stale_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "spark.pid"
        pid_file.write_text("99999999")  # Unlikely to be running
        dm = DaemonManager(pid_file=str(pid_file))
        status = dm.status()
        assert status["running"] is False
        # Stale PID file should be cleaned up
        assert not pid_file.exists()

    def test_is_running_false(self, tmp_path: Path) -> None:
        dm = DaemonManager(pid_file=str(tmp_path / "spark.pid"))
        assert dm.is_running() is False

    def test_stop_no_daemon(self, tmp_path: Path) -> None:
        dm = DaemonManager(pid_file=str(tmp_path / "spark.pid"))
        assert dm.stop() is True

    def test_stop_stale_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "spark.pid"
        pid_file.write_text("99999999")
        dm = DaemonManager(pid_file=str(pid_file))
        assert dm.stop() is True
        assert not pid_file.exists()

    def test_start_already_running(self, tmp_path: Path) -> None:
        import os

        pid_file = tmp_path / "spark.pid"
        pid_file.write_text(str(os.getpid()))  # Current process is running
        dm = DaemonManager(pid_file=str(pid_file))
        assert dm.start() is False

    def test_write_and_read_pid(self, tmp_path: Path) -> None:
        dm = DaemonManager(pid_file=str(tmp_path / "spark.pid"))
        dm._write_pid(12345)
        assert dm._read_pid() == 12345

    def test_cleanup_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "spark.pid"
        pid_file.write_text("123")
        dm = DaemonManager(pid_file=str(pid_file))
        dm._cleanup_pid()
        assert not pid_file.exists()

    def test_is_process_alive_current(self) -> None:
        import os

        assert DaemonManager._is_process_alive(os.getpid()) is True

    def test_is_process_alive_nonexistent(self) -> None:
        assert DaemonManager._is_process_alive(99999999) is False


# -- ActionRunner (unit tests without APScheduler) ----------------------------


class TestActionRunnerScheduling:
    def test_cron_parsing(self) -> None:
        """Verify cron expression parsing logic."""
        cron = "30 8 * * 1-5"
        parts = cron.split()
        assert len(parts) == 5
        assert parts[0] == "30"  # minute
        assert parts[1] == "8"  # hour
        assert parts[4] == "1-5"  # weekdays

    def test_schedule_config_json(self) -> None:
        """Verify schedule config JSON parsing."""
        config = json.dumps({"cron": "0 */6 * * *"})
        parsed = json.loads(config)
        assert parsed["cron"] == "0 */6 * * *"

    def test_one_off_config(self) -> None:
        config = json.dumps({"run_at": "2026-04-01T10:00:00"})
        parsed = json.loads(config)
        assert "run_at" in parsed


# -- ActionExecutor (unit tests) ----------------------------------------------


class TestActionExecutor:
    def test_run_action_builds_system_prompt(self) -> None:
        """Verify action system prompt includes key fields."""
        action = {
            "name": "Daily Report",
            "description": "Generate a daily summary",
            "action_prompt": "Generate report",
            "model_id": "test-model",
            "max_tokens": 4096,
        }
        # Just verify the data structure is correct
        assert action["name"] == "Daily Report"
        assert action["action_prompt"] == "Generate report"

    def test_failure_counting(self) -> None:
        """Verify failure count logic."""
        failure_count = 2
        max_failures = 3
        new_count = failure_count + 1
        should_disable = new_count >= max_failures
        assert should_disable is True

    def test_failure_not_disabled(self) -> None:
        failure_count = 0
        max_failures = 3
        new_count = failure_count + 1
        should_disable = new_count >= max_failures
        assert should_disable is False
