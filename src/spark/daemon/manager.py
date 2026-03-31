"""Daemon lifecycle management — start, stop, status."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PID_FILE = "spark_daemon.pid"


class DaemonManager:
    """Manages the Spark daemon process lifecycle."""

    def __init__(self, pid_file: str = _DEFAULT_PID_FILE) -> None:
        self._pid_file = Path(pid_file)

    def start(self, *, foreground: bool = False) -> bool:
        """Start the daemon. Returns True on success."""
        if self.is_running():
            logger.warning("Daemon is already running (PID %s)", self._read_pid())
            return False

        if foreground:
            return self._run_foreground()
        return self._run_background()

    def stop(self, *, timeout: int = 10) -> bool:
        """Stop the daemon gracefully. Returns True if stopped."""
        pid = self._read_pid()
        if pid is None:
            logger.info("No daemon PID file found")
            return True

        if not self._is_process_alive(pid):
            self._cleanup_pid()
            return True

        logger.info("Stopping daemon (PID %d)...", pid)

        # Send SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            self._cleanup_pid()
            return True

        # Wait for graceful shutdown
        for _ in range(timeout * 10):
            if not self._is_process_alive(pid):
                self._cleanup_pid()
                logger.info("Daemon stopped")
                return True
            time.sleep(0.1)

        # Force kill
        logger.warning("Daemon did not stop gracefully, forcing...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        self._cleanup_pid()
        return True

    def status(self) -> dict:
        """Get daemon status."""
        pid = self._read_pid()
        if pid is None:
            return {"running": False, "pid": None}

        alive = self._is_process_alive(pid)
        if not alive:
            self._cleanup_pid()
        return {"running": alive, "pid": pid if alive else None}

    def is_running(self) -> bool:
        """Check if the daemon is currently running."""
        return self.status()["running"]

    def _run_foreground(self) -> bool:
        """Run the daemon in the foreground (for debugging)."""
        self._write_pid(os.getpid())
        try:
            from spark.daemon.app import run_daemon

            run_daemon()
            return True
        finally:
            self._cleanup_pid()

    def _run_background(self) -> bool:
        """Start the daemon as a detached background process."""
        cmd = [sys.executable, "-m", "spark.daemon.app"]

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True

        log_file = self._pid_file.with_suffix(".log")
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                **kwargs,
            )

        self._write_pid(proc.pid)

        # Verify it started
        time.sleep(1)
        if self._is_process_alive(proc.pid):
            logger.info("Daemon started (PID %d)", proc.pid)
            return True

        self._cleanup_pid()
        logger.error("Daemon failed to start")
        return False

    def _write_pid(self, pid: int) -> None:
        self._pid_file.write_text(str(pid))

    def _read_pid(self) -> int | None:
        if not self._pid_file.exists():
            return None
        try:
            return int(self._pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def _cleanup_pid(self) -> None:
        try:
            self._pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
