"""System tray daemon for Spark Autonomous Actions.

Provides a persistent system tray icon (macOS + Windows) that:
- Shows blue icon when active, grey when paused
- Menu: Open Spark, Pause/Resume, Quit
- Runs the action scheduler independently of the web UI
- Survives web UI shutdown
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _utc_to_local(iso_str: str) -> str:
    """Convert a UTC ISO datetime string to local time display."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        return local_dt.strftime("%d %b, %H:%M")
    except Exception:
        return iso_str[:16]


def _create_icon_image(color: str = "blue") -> Any:
    """Create the tray icon as a PIL Image.

    Renders a lightning bolt (Spark icon).
    Blue = active, grey = paused.
    """
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if color == "blue":
        bolt_color = (90, 170, 232, 255)  # #5aaae8
        bg_color = (8, 14, 30, 255)  # #080e1e
    else:
        bolt_color = (128, 128, 128, 255)
        bg_color = (40, 40, 40, 255)

    # Background rounded square
    draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=12, fill=bg_color)

    # Lightning bolt polygon
    bolt = [
        (35, 8),  # top right
        (18, 30),  # middle left
        (30, 30),  # middle center-left
        (25, 56),  # bottom left
        (46, 28),  # middle right
        (34, 28),  # middle center-right
    ]
    draw.polygon(bolt, fill=bolt_color)

    return img


class SparkTrayDaemon:
    """System tray daemon for autonomous action execution."""

    def __init__(self) -> None:
        self._paused = False
        self._running = True
        self._icon: Any = None
        self._runner: Any = None
        self._runner_thread: threading.Thread | None = None
        self._action_stats: dict[str, Any] = {
            "total": 0,
            "last_run": None,
            "next_run": None,
        }
        self._stats_thread: threading.Thread | None = None

    def _pid_file(self) -> Path:
        from spark.core.application import _get_data_path

        return _get_data_path() / "spark_daemon_tray.pid"

    def run(self) -> None:
        """Start the tray icon and action runner."""
        import pystray

        # Write PID file
        pid_path = self._pid_file()
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))

        logger.info("Starting Spark Autonomous Action Daemon (system tray, PID %d)", os.getpid())

        self._icon = pystray.Icon(
            name="Spark Daemon",
            icon=_create_icon_image("blue"),
            title="Spark Autonomous Action Daemon",
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda item: f"Actions: {self._action_stats['total']}",
                    None,
                    enabled=False,
                ),
                pystray.MenuItem(
                    lambda item: f"Last run: {self._action_stats['last_run'] or 'Never'}",
                    None,
                    enabled=False,
                ),
                pystray.MenuItem(
                    lambda item: f"Next run: {self._action_stats['next_run'] or 'None scheduled'}",
                    None,
                    enabled=False,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Spark", self._on_open),
                pystray.MenuItem("Open Log Folder", self._on_open_logs),
                pystray.MenuItem(
                    lambda item: "Resume" if self._paused else "Pause",
                    self._on_pause_toggle,
                ),
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

        # Start action runner in background thread
        self._runner_thread = threading.Thread(target=self._run_scheduler, daemon=True)
        self._runner_thread.start()

        # Start stats polling thread
        self._stats_thread = threading.Thread(target=self._poll_stats, daemon=True)
        self._stats_thread.start()

        # Run the tray icon (blocks on macOS)
        try:
            self._icon.run()
        finally:
            # Clean up PID file
            try:
                self._pid_file().unlink(missing_ok=True)
            except Exception:
                pass
            logger.info("Tray daemon stopped")

    def _on_open(self, icon: Any, item: Any) -> None:
        """Open the Spark web UI. If already running, opens the existing URL."""
        import webbrowser

        try:
            from spark.core.application import _get_data_path

            url_file = _get_data_path() / "spark_url.txt"

            # Check if Spark is already running via the URL file
            if url_file.exists():
                url = url_file.read_text().strip()
                if url:
                    try:
                        import httpx

                        resp = httpx.get(f"{url}/loading/api/status", timeout=2)
                        if resp.status_code == 200:
                            # Spark is running — open the actions page
                            webbrowser.open(f"{url}/actions")
                            logger.info("Opened existing Spark at %s", url)
                            return
                    except Exception:
                        pass  # Spark not responding, start a new one

            # Start Spark as a detached process
            cmd = [sys.executable, "-m", "spark.launch"]
            if sys.platform == "win32":
                subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                )
            else:
                subprocess.Popen(cmd, start_new_session=True)
            logger.info("Started Spark application")

        except Exception as e:
            logger.error("Failed to open Spark: %s", e)

    def _poll_stats(self) -> None:
        """Periodically read action stats from the database."""
        while self._running:
            try:
                from spark.core.application import _get_config_path, _get_data_path
                from spark.database.backends import SQLiteBackend
                from spark.database.connection import DatabaseConnection

                db_path = _get_data_path() / "spark.db"
                if not db_path.exists():
                    time.sleep(30)
                    continue

                backend = SQLiteBackend(str(db_path))
                conn = DatabaseConnection(backend)
                # Ensure schema exists (idempotent)
                from spark.database.schema import initialise_schema

                initialise_schema(conn)
                try:
                    # Total enabled actions
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM autonomous_actions WHERE is_enabled = 1"
                    )
                    row = cursor.fetchone()
                    total = row[0] if row else 0
                    if total != self._action_stats.get("total"):
                        logger.info("Action stats updated: %d enabled actions", total)
                    self._action_stats["total"] = total

                    # Last run time (most recent completed run)
                    cursor = conn.execute(
                        "SELECT completed_at FROM action_runs WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        self._action_stats["last_run"] = _utc_to_local(str(row[0]))
                    else:
                        self._action_stats["last_run"] = None

                    # Next run time
                    cursor = conn.execute(
                        "SELECT next_run_at FROM autonomous_actions WHERE is_enabled = 1 AND next_run_at IS NOT NULL ORDER BY next_run_at ASC LIMIT 1"
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        self._action_stats["next_run"] = _utc_to_local(str(row[0]))
                    else:
                        self._action_stats["next_run"] = None

                finally:
                    conn.close()

                # Update tray icon title with summary
                if self._icon and not self._paused:
                    total = self._action_stats["total"]
                    self._icon.title = f"Spark Daemon — {total} action{'s' if total != 1 else ''}"

            except Exception as e:
                logger.debug("Stats poll error: %s", e)

            time.sleep(15)

    def _on_open_logs(self, icon: Any, item: Any) -> None:
        """Open the log folder in the system file browser."""
        try:
            from konfig.paths import log_dir

            log_path = log_dir("spark")
            log_path.mkdir(parents=True, exist_ok=True)

            if sys.platform == "darwin":
                subprocess.Popen(["open", str(log_path)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", str(log_path)])
            else:
                subprocess.Popen(["xdg-open", str(log_path)])
        except Exception as e:
            logger.error("Failed to open log folder: %s", e)

    def _on_pause_toggle(self, icon: Any, item: Any) -> None:
        """Toggle pause/resume."""
        self._paused = not self._paused
        icon.icon = _create_icon_image("grey" if self._paused else "blue")
        icon.title = "Spark Daemon (Paused)" if self._paused else "Spark Autonomous Action Daemon"
        logger.info("Daemon %s", "paused" if self._paused else "resumed")

    def _on_quit(self, icon: Any, item: Any) -> None:
        """Quit the daemon."""
        logger.info("Daemon quit requested")
        self._running = False
        icon.stop()

    def _run_scheduler(self) -> None:
        """Run the action scheduler loop."""
        try:
            from konfig import AppContext

            import spark
            from spark.core.application import _default_settings, _get_config_path

            config_path = _get_config_path()
            if not config_path.exists():
                logger.error("No config.yaml found — daemon scheduler idle")
                return

            import asyncio

            async def _init_and_run() -> None:
                async with AppContext(
                    name="Spark Daemon",
                    version=spark.__version__,
                    config_file=str(config_path),
                    defaults=_default_settings(),
                    env_prefix="SPARK",
                ) as ctx:
                    from spark.scheduler.runner import ActionRunner

                    daemon_id = f"tray-{os.getpid()}"
                    runner = ActionRunner(ctx, daemon_id)
                    runner.start()

                    while self._running:
                        if self._paused:
                            time.sleep(1)
                            continue
                        time.sleep(5)

                    runner.stop()

            asyncio.run(_init_and_run())

        except Exception as e:
            logger.error("Daemon scheduler error: %s", e)


def run_tray_daemon() -> None:
    """Entry point for the tray daemon process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    daemon = SparkTrayDaemon()
    daemon.run()


if __name__ == "__main__":
    run_tray_daemon()
