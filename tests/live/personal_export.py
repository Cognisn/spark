"""Seed a synthetic personal-mode user and export them with the real ``spark export`` (live suite).

    python -m tests.live.personal_export TARGET.zip

Run by ``test_rc1`` in a child process whose environment points every
per-user folder (``APPDATA``, ``LOCALAPPDATA``, ``HOME``, the XDG paths) into
the sandbox and keeps secrets in an encrypted file there with a dev-only key,
so nothing touches this machine's real Spark data or keychain. It loads the
personal instance exactly as the CLI does (which brings the database to the
current schema), writes ``tests.bundle_seed``'s synthetic user into it, then
runs the CLI's own export to TARGET.
"""

from __future__ import annotations

import asyncio
import sys

from spark import mode
from spark.bundles import cli
from tests.bundle_seed import seed


async def _seed(loaded: cli.Loaded) -> None:
    from spark.db import Database

    db = await Database.open(loaded.database)
    try:
        await seed(db, loaded.user_guid, loaded.sources.files)
    finally:
        await db.close()


def main(argv: list[str]) -> int:
    (target,) = argv
    loaded = cli._load_personal()
    assert loaded.database.dialect is mode.Dialect.SQLITE, loaded.database
    asyncio.run(_seed(loaded))
    return cli.run_export(["--output", target])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
