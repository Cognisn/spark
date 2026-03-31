"""Spark application bootstrap and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from konfig import AppContext
from konfig.paths import config_dir, data_dir

import spark

logger = logging.getLogger(__name__)

_APP_ID = "spark"
_RESOURCES = Path(__file__).resolve().parent.parent / "resources"


def _get_config_path() -> Path:
    """Return the platform-conventional config file path."""
    return config_dir(_APP_ID) / "config.yaml"


def _get_data_path() -> Path:
    """Return the platform-conventional data directory."""
    return data_dir(_APP_ID)


def _get_default_db_path() -> str:
    """Return the default SQLite database path inside the data directory."""
    return str(_get_data_path() / "spark.db")


def _ensure_config(config_path: Path) -> bool:
    """Ensure a config.yaml exists. Returns True if freshly created (first run)."""
    if config_path.exists():
        return False

    config_path.parent.mkdir(parents=True, exist_ok=True)

    template = _RESOURCES / "config.yaml.template"
    if template.exists():
        shutil.copy(template, config_path)
        logger.info("Created default config.yaml from template")
    else:
        config_path.write_text("# Spark configuration — see documentation for options\n")
        logger.info("Created empty config.yaml")
    return True


def run() -> None:
    """Main entry point — initialise konfig and start the web server."""
    config_path = _get_config_path()
    first_run = _ensure_config(config_path)

    async def _start() -> None:
        async with AppContext(
            name="Spark",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=_default_settings(),
            env_prefix="SPARK",
        ) as ctx:
            _log_startup_paths(ctx, config_path)

            from spark.web.server import create_and_serve

            await create_and_serve(ctx, first_run=first_run)

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        logger.info("Spark shut down by user")


def _log_startup_paths(ctx: AppContext, config_path: Path) -> None:
    """Log key file locations at startup."""
    db_type = ctx.settings.get("database.type", "sqlite")
    if db_type == "sqlite":
        db_path = _get_data_path() / "spark.db"
        logger.info("Database: %s (SQLite)", db_path)
    else:
        db_host = ctx.settings.get("database.host", "localhost")
        db_port = ctx.settings.get("database.port", "")
        db_name = ctx.settings.get("database.name", "spark")
        logger.info("Database: %s://%s:%s/%s", db_type, db_host, db_port, db_name)

    logger.info("Config:   %s", config_path.resolve())
    logger.info("Data:     %s", _get_data_path())

    if hasattr(ctx, "log_manager") and ctx.log_manager:
        logger.info("Logs:     %s", ctx.log_manager.run_dir)


def _default_settings() -> dict:
    """Sensible defaults so Spark can boot with an empty config."""
    return {
        "logging": {
            "level": "INFO",
            "format": "text",
            "retention_runs": 10,
            "console_output": "auto",
        },
        "database": {
            "type": "sqlite",
            "path": _get_default_db_path(),
        },
        "interface": {
            "host": "127.0.0.1",
            "ssl": {"enabled": False},
            "session_timeout_minutes": 60,
            "browser_heartbeat": {
                "enabled": True,
                "interval_seconds": 30,
                "miss_threshold": 3,
            },
        },
        "providers": {},
        "conversation": {
            "rollup_threshold": 0.3,
            "rollup_summary_ratio": 0.3,
            "emergency_rollup_threshold": 0.95,
            "max_tool_iterations": 25,
            "max_tool_selections": 30,
            "max_tool_result_tokens": 4000,
        },
        "tool_permissions": {"auto_approve": False},
        "embedded_tools": {
            "filesystem": {"enabled": True, "mode": "read", "allowed_paths": []},
            "documents": {"enabled": True, "mode": "read", "max_file_size_mb": 50},
            "archives": {"enabled": True, "mode": "list"},
            "web": {"enabled": True},
        },
    }
