"""Spark application bootstrap and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

from konfig import AppContext
from konfig.paths import config_dir, data_dir

import spark
from spark import mode

logger = logging.getLogger(__name__)


def _ensure_macos_path() -> None:
    """Ensure common tool directories are on PATH for macOS app bundles.

    When launched from Finder (e.g. as a PyApp binary), the process inherits a
    minimal PATH that excludes directories like /usr/local/bin and Homebrew
    paths where tools such as docker, npx, and node typically live.  This
    prevents MCP stdio servers (and any other subprocess) from finding their
    commands.

    This is a no-op on non-macOS platforms.
    """
    if sys.platform != "darwin":
        return

    # Directories commonly missing from Finder-launched processes.
    extra_dirs = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        str(Path.home() / ".docker" / "bin"),
    ]

    current = os.environ.get("PATH", "")
    current_set = set(current.split(os.pathsep))

    additions = [d for d in extra_dirs if d not in current_set and Path(d).is_dir()]
    if additions:
        os.environ["PATH"] = current + os.pathsep + os.pathsep.join(additions)
        logger.debug("Extended PATH with: %s", ", ".join(additions))


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


def configure_runtime(settings: object, secrets: object | None = None) -> mode.RuntimeSettings:
    """Validate the mode and database settings and set the process-wide mode.

    With ``secrets`` (konfig's store) a ``secret://`` ``database.url`` is resolved
    through it. Raises :class:`spark.mode.ModeConfigError` when the settings are
    invalid (for example server mode without a PostgreSQL ``database.url``); Spark
    must not start.
    """
    from spark.instance_settings import startup

    view = (
        startup.database_view(settings, secrets)  # type: ignore[arg-type]
        if secrets is not None
        else settings
    )
    runtime = mode.resolve(
        view,  # type: ignore[arg-type]
        default_sqlite_path=Path(_get_default_db_path()),
    )
    mode.configure(runtime)
    from spark import files

    files.configure(
        settings,  # type: ignore[arg-type]
        data_dir=_get_data_path(),
        runtime_mode=runtime.mode,
    )
    return runtime


def validate_startup(ctx: AppContext, config_path: Path) -> mode.RuntimeSettings:
    """Migrate 0.2 secret names, validate the start-up configuration and set the mode.

    Raises :class:`spark.mode.ModeConfigError` (a
    :class:`spark.instance_settings.startup.StartupConfigError` for the start-up
    keys) with a message naming the setting; Spark must not start.
    """
    from spark.instance_settings import secrets as secrets_mod
    from spark.instance_settings import startup

    if secrets_mod.migrate_config_file(config_path, ctx.secrets):
        ctx.settings.reload()
    config = startup.validate(
        ctx.settings, ctx.secrets, default_sqlite_path=Path(_get_default_db_path())
    )
    for warning in config.warnings:
        logger.warning("%s", warning)
    mode.configure(config.runtime)
    from spark import files

    files.configure(
        ctx.settings,  # type: ignore[arg-type]
        data_dir=_get_data_path(),
        runtime_mode=config.runtime.mode,
    )
    return config.runtime


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
    _ensure_macos_path()
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
            validate_startup(ctx, config_path)
            _log_startup_paths(ctx, config_path)

            from spark.web.server import create_and_serve

            await create_and_serve(ctx, first_run=first_run)

    try:
        # On Windows, uvicorn needs the SelectorEventLoop (ProactorEventLoop has issues)
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        asyncio.run(_start())
    except KeyboardInterrupt:
        logger.info("Spark shut down by user")
    except mode.ModeConfigError as exc:
        logger.error("Spark cannot start: %s", exc)
        raise SystemExit(2) from None
    except Exception:
        logger.exception("Spark failed to start")
        raise


def run_migrations(runtime: mode.RuntimeSettings) -> dict[str, str | None]:
    """Run SPARK's migration chain, then warden's (server mode), on the configured database.

    A chain that does not apply to the database (warden on SQLite) reports None.
    """
    from spark.db import migrate
    from spark.identity import migrations as identity_migrations

    identity_migrations.register()
    return migrate.migrate_all(runtime.database)


def run_migrate() -> int:
    """``spark migrate``: load the configuration, migrate, report, and return an exit code."""
    from spark.db.migrate import MigrationError

    config_path = _get_config_path()
    _ensure_config(config_path)

    async def _load() -> mode.RuntimeSettings:
        async with AppContext(
            name="Spark",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=_default_settings(),
            env_prefix="SPARK",
        ) as ctx:
            from spark.instance_settings.secrets import migrate_config_file

            if migrate_config_file(config_path, ctx.secrets):
                ctx.settings.reload()
            return configure_runtime(ctx.settings, ctx.secrets)

    try:
        runtime = asyncio.run(_load())
        results = run_migrations(runtime)
    except (mode.ModeConfigError, MigrationError) as exc:
        print(f"spark migrate: {exc}", file=sys.stderr)
        return 2
    for chain, revision in results.items():
        if revision is not None:  # None: the chain does not apply (warden in personal mode)
            print(f"{chain}: {revision}")
    return 0


_IDENTITY_USAGE = "usage: spark identity create-admin --email EMAIL [--link-user-guid GUID]"


def run_identity(args: list[str]) -> int:
    """``spark identity create-admin``: create the first administrator (server mode).

    Reads the configuration as ``spark web`` does, needs the database at the
    current schema (``spark migrate`` first), starts warden's runtime (which
    creates a signing key and the TOTP key if they are missing) and returns an
    exit code.
    """
    from warden.identity import RoleError, UserError

    from spark.database.user_identities import IdentityConflict
    from spark.db.migrate import MigrationError
    from spark.identity import bootstrap

    if not args or args[0] != "create-admin":
        print(_IDENTITY_USAGE, file=sys.stderr)
        return 2
    options = bootstrap.parse_create_admin(args[1:])
    config_path = _get_config_path()
    _ensure_config(config_path)

    async def _create() -> str:
        from spark.db import Database, migrate
        from spark.identity import migrations as identity_migrations
        from spark.identity.runtime import IdentityRuntime

        async with AppContext(
            name="Spark",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=_default_settings(),
            env_prefix="SPARK",
        ) as ctx:
            runtime = validate_startup(ctx, config_path)
            if runtime.mode is not mode.Mode.SERVER:
                raise mode.ModeConfigError(
                    "spark identity needs server mode: personal mode has one local user"
                )
            identity_migrations.register()
            await asyncio.to_thread(migrate.prepare, runtime.database, auto_migrate=False)
            database = await Database.open(runtime.database)
            try:
                identity = await IdentityRuntime.start(database, ctx.settings, ctx.secrets)
                try:
                    result = await bootstrap.create_admin(
                        identity,
                        database,
                        options.email,
                        bootstrap.read_password,
                        link_user_guid=options.link_user_guid,
                    )
                finally:
                    identity.close()
            finally:
                await database.close()
            return bootstrap.describe(result)

    try:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print(asyncio.run(_create()))
    except (
        mode.ModeConfigError,
        MigrationError,
        UserError,
        RoleError,
        IdentityConflict,
        ValueError,
    ) as exc:
        print(f"spark identity create-admin: {exc}", file=sys.stderr)
        return 2
    return 0


def _log_startup_paths(ctx: AppContext, config_path: Path) -> None:
    """Log key file locations at startup."""
    runtime = mode.current()
    logger.info("Mode:     %s", runtime.mode.value)
    if runtime.database.sqlite_path is not None:
        logger.info("Database: %s (SQLite)", runtime.database.sqlite_path)
    else:
        logger.info("Database: PostgreSQL")

    logger.info("Config:   %s", config_path.resolve())
    logger.info("Data:     %s", _get_data_path())

    if hasattr(ctx, "log_manager") and ctx.log_manager:
        logger.info("Logs:     %s", ctx.log_manager.run_dir)


def _default_settings() -> dict:
    """Sensible defaults so Spark can boot with an empty config."""
    return {
        "mode": "personal",
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
            "port": 0,
            "open_browser": True,
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
            "web": {"enabled": True, "search_engine": "duckduckgo"},
            "system_commands": {
                "enabled": False,
                "timeout": 30,
                "max_timeout": 300,
                "max_output_chars": 50000,
                "blocked_commands": [],
                "require_approval": True,
            },
            "email": {
                "enabled": False,
                "host": "",
                "port": 587,
                "username": "",
                "password": "",
                "sender": "",
                "use_tls": True,
                "max_attachment_mb": 25,
                "require_approval": True,
            },
            "agents": {
                "enabled": False,
                "default_mode": "orchestrator",
                "model_selection": "same",
                "max_concurrent": 5,
                "max_depth": 2,
                "max_iterations": 15,
            },
        },
    }
