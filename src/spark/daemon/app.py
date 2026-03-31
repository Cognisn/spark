"""Daemon application — long-running process for autonomous actions."""

from __future__ import annotations

import logging
import signal
import threading
import time
import uuid

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
        from konfig import AppContext
        import spark

        # Boot konfig
        from pathlib import Path

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
                from spark.scheduler.runner import ActionRunner

                runner = ActionRunner(ctx, daemon_id)
                runner.start()

                logger.info("Daemon running, waiting for shutdown signal...")
                while not _shutdown.is_set():
                    _shutdown.wait(timeout=5.0)

                logger.info("Shutdown signal received")
                runner.stop()

        asyncio.run(_run())

    except Exception as e:
        logger.error("Daemon error: %s", e, exc_info=True)
    finally:
        logger.info("Spark daemon stopped")


if __name__ == "__main__":
    run_daemon()
