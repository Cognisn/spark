"""Daemon application — long-running process for autonomous actions."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
import uuid

from spark.daemon.guard import ServerModeRefused

logger = logging.getLogger(__name__)

_shutdown = threading.Event()


def run_daemon() -> None:
    """Main daemon entry point."""
    daemon_id = str(uuid.uuid4())[:8]
    logger.info("Spark daemon starting (id=%s)", daemon_id)

    # Register signal handlers
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: _shutdown.set())

    try:
        # Boot konfig
        from pathlib import Path

        from konfig import AppContext

        import spark

        config_path = Path("config.yaml")
        if not config_path.exists():
            logger.error("No config.yaml found — daemon cannot start")
            return

        import asyncio

        async def _run() -> None:
            async with AppContext(
                name="Spark Daemon",
                version=spark.__version__,
                config_file=str(config_path),
                env_prefix="SPARK",
            ) as ctx:
                from spark.core.application import configure_runtime
                from spark.daemon.guard import refuse_server_mode
                from spark.db.standalone import open_standalone
                from spark.mode import Mode
                from spark.scheduler.runner import ActionRunner

                runtime = configure_runtime(ctx.settings)
                refuse_server_mode(runtime)
                handle = await asyncio.to_thread(
                    open_standalone,
                    runtime.database,
                    auto_migrate=runtime.mode is Mode.PERSONAL,
                )
                try:
                    runner = ActionRunner(ctx, daemon_id, handle.sync_db)
                    runner.start()

                    logger.info("Daemon running, waiting for shutdown signal...")
                    while not _shutdown.is_set():
                        _shutdown.wait(timeout=5.0)

                    logger.info("Shutdown signal received")
                    runner.stop()
                finally:
                    await asyncio.to_thread(handle.close)

        asyncio.run(_run())

    except ServerModeRefused as exc:
        logger.error("%s", exc)
        print(f"spark daemon: {exc}", file=sys.stderr)
    except Exception as e:
        logger.error("Daemon error: %s", e, exc_info=True)
    finally:
        logger.info("Spark daemon stopped")


if __name__ == "__main__":
    run_daemon()
