"""``spark export [--output PATH]``: write the personal user's bundle to a file.

Reads the configuration as ``spark web`` does, refuses server mode (a server's
users are imported, not exported), brings the database to the current schema
as personal mode always does, and writes the bundle next to the path given (by
default ``spark-bundle-<date>-<id>.zip`` in the working directory) before
moving it into place. An existing file is never overwritten.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import spark
from spark import mode
from spark.bundles import format as bf
from spark.bundles.exporter import ExportReport, ExportSources, export_bundle
from spark.bundles.personal import bundle_filename, personal_sources

__all__ = ["SERVER_REFUSAL", "USAGE", "Loaded", "run_export"]

USAGE = "usage: spark export [--output PATH]"
SERVER_REFUSAL = (
    "spark export works in personal mode only: a server's users are moved with an "
    "administrator's import, not exported"
)


@dataclass(frozen=True, slots=True)
class Loaded:
    """What an export needs from the configuration."""

    database: mode.DatabaseSettings
    user_guid: str
    sources: ExportSources


def _load_personal() -> Loaded:
    """Read the configuration as ``spark web`` does; personal mode only."""
    from konfig import AppContext

    from spark.core import application
    from spark.core.user_guid import get_user_guid
    from spark.db import migrate

    config_path = application._get_config_path()
    application._ensure_config(config_path)

    async def load() -> Loaded:
        async with AppContext(
            name="Spark",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=application._default_settings(),
            env_prefix="SPARK",
        ) as ctx:
            runtime = application.validate_startup(ctx, config_path)
            if runtime.mode is not mode.Mode.PERSONAL:
                raise mode.ModeConfigError(SERVER_REFUSAL)
            await asyncio.to_thread(migrate.prepare, runtime.database, auto_migrate=True)
            return Loaded(
                database=runtime.database,
                user_guid=get_user_guid(ctx),
                sources=personal_sources(ctx.settings, ctx.secrets),
            )

    return asyncio.run(load())


def _parse(args: Sequence[str]) -> Path | None | str:
    """The output path, None for the default, or an error message."""
    if not args:
        return None
    if len(args) == 2 and args[0] in ("--output", "-o") and args[1]:
        return Path(args[1])
    return USAGE


async def _write(loaded: Loaded, target: Path | None) -> tuple[Path, ExportReport]:
    from spark.db import Database

    folder = (target.parent if target else Path.cwd()).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    handle, staging = tempfile.mkstemp(prefix=".spark-bundle-", suffix=".part", dir=folder)
    try:
        db = await Database.open(loaded.database)
        try:
            with os.fdopen(handle, "wb") as out:
                report = await export_bundle(
                    db, loaded.user_guid, out, loaded.sources, spark_version=spark.__version__
                )
        finally:
            await db.close()
        final = target.resolve() if target else folder / bundle_filename(report.bundle_id)
        if final.exists():
            raise FileExistsError(str(final))
        os.replace(staging, final)
        return final, report
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def run_export(args: Sequence[str], *, load: Callable[[], Loaded] = _load_personal) -> int:
    """``spark export``; returns an exit code."""
    parsed = _parse(args)
    if isinstance(parsed, str):
        print(parsed, file=sys.stderr)
        return 2
    if parsed is not None and parsed.exists():
        print(f"spark export: {parsed} already exists; choose another path", file=sys.stderr)
        return 2
    try:
        loaded = load()
        path, report = asyncio.run(_write(loaded, parsed))
    except mode.ModeConfigError as exc:
        print(f"spark export: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"spark export: {exc} already exists; choose another path", file=sys.stderr)
        return 2
    print(f"Wrote {path} (bundle {report.bundle_id}, format version {bf.FORMAT_VERSION})")
    for table, count in sorted(report.counts.items()):
        print(f"  {table}: {count}")
    if report.redactions:
        print(f"  credentials redacted: {report.redactions}")
    for item in report.files_left_out + report.skills_left_out:
        print(f"  left out: {item}")
    return 0
