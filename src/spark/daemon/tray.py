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
        self._db_handle: Any = None

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

        # Fetch stats immediately so the menu is populated from the start
        self._fetch_stats_once()

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

    def _fetch_stats_once(self) -> None:
        """Read action stats from the database once."""
        try:
            handle = self._db_handle
            if handle is None:
                return  # the scheduler has not opened the database yet
            from spark.database import autonomous_actions

            stats = handle.sync_db.call(autonomous_actions.action_stats)
            total = stats["total"]
            if total != self._action_stats.get("total"):
                logger.info("Action stats updated: %d enabled actions", total)
            self._action_stats["total"] = total
            self._action_stats["last_run"] = (
                _utc_to_local(stats["last_run"]) if stats["last_run"] else None
            )
            self._action_stats["next_run"] = (
                _utc_to_local(stats["next_run"]) if stats["next_run"] else None
            )

            # Update tray icon title and refresh menu so stats are visible
            if self._icon and not self._paused:
                total = self._action_stats["total"]
                self._icon.title = f"Spark Daemon — {total} action{'s' if total != 1 else ''}"
            if self._icon:
                self._icon.update_menu()

        except Exception as e:
            logger.debug("Stats fetch error: %s", e)

    def _poll_stats(self) -> None:
        """Periodically read action stats from the database."""
        while self._running:
            self._fetch_stats_once()
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
                    from spark.core.application import configure_runtime
                    from spark.db.standalone import open_standalone
                    from spark.mode import Mode
                    from spark.scheduler.runner import ActionRunner

                    runtime = configure_runtime(ctx.settings)
                    handle = await asyncio.to_thread(
                        open_standalone,
                        runtime.database,
                        auto_migrate=runtime.mode is Mode.PERSONAL,
                    )
                    self._db_handle = handle
                    try:
                        daemon_id = f"tray-{os.getpid()}"
                        runner = ActionRunner(ctx, daemon_id, handle.sync_db)
                        runner.start()

                        while self._running:
                            if self._paused:
                                await asyncio.sleep(1)
                                continue
                            await asyncio.sleep(5)

                        runner.stop()
                    finally:
                        self._db_handle = None
                        await asyncio.to_thread(handle.close)

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
