"""warden's migration chain, run by ``spark migrate`` after SPARK's own.

warden keeps its tables and its version table (``warden.alembic_version``) in
its own ``warden`` schema, so its chain is independent of SPARK's. It is
PostgreSQL only: on SQLite (personal mode) the chain is skipped and reports
``None``.

:func:`register` adds the chain to :mod:`spark.db.migrate`. Every process that
migrates or checks the schema calls it first (``spark migrate``, the web
lifespan and the worker), so all of them agree; in server mode a database whose
warden chain is not at head is then refused like SPARK's own
(:class:`spark.db.migrate.MigrationRequired`).
"""

from __future__ import annotations

from importlib import resources

import warden.db
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from spark.db import migrate
from spark.db.migrate import Chain, DatabaseTooNew, MigrationError, MigrationRequired
from spark.mode import DatabaseSettings, Dialect

__all__ = ["CHAIN_NAME", "WARDEN_CHAIN", "check_at_head", "register", "upgrade", "warden_head"]

CHAIN_NAME = "warden"


def _url(settings: DatabaseSettings) -> str | None:
    if settings.dialect is not Dialect.POSTGRESQL:
        return None
    if not settings.url:
        raise MigrationError("PostgreSQL needs a database URL")
    return settings.url


def warden_head() -> str:
    """The head revision of the warden this SPARK depends on."""
    heads = warden.db.heads()
    if len(heads) != 1:
        raise MigrationError(f"warden's chain must have exactly one head, found {heads}")
    return heads[0]


def _known(revision: str) -> bool:
    cfg = Config()
    cfg.set_main_option("script_location", str(resources.files("warden.db").joinpath("migrations")))
    try:
        return ScriptDirectory.from_config(cfg).get_revision(revision) is not None
    except CommandError:
        return False


def upgrade(settings: DatabaseSettings) -> str | None:
    """Bring warden's chain to head; return its revision (None on SQLite, which has no warden)."""
    url = _url(settings)
    if url is None:
        return None
    warden.db.migrate(url)
    return warden.db.current_revision(url)


def check_at_head(settings: DatabaseSettings) -> None:
    """Refuse a PostgreSQL database whose warden chain is not exactly at head (SQLite passes)."""
    url = _url(settings)
    if url is None:
        return
    revision = warden.db.current_revision(url)
    wanted = warden_head()
    if revision == wanted:
        return
    if revision is not None and not _known(revision):
        raise DatabaseTooNew(
            f"warden's schema is at revision {revision}, which this SPARK does not know "
            f"(its newest is {wanted}); upgrade SPARK"
        )
    raise MigrationRequired(
        f"warden's schema is at revision {revision or 'none'} but this SPARK needs "
        f"{wanted}; run `spark migrate`"
    )


WARDEN_CHAIN = Chain(name=CHAIN_NAME, upgrade=upgrade, check=check_at_head)


def register() -> None:
    """Register warden's chain after SPARK's; calling it again changes nothing."""
    if any(chain.name == CHAIN_NAME for chain in migrate.registered_chains()):
        return
    migrate.register_chain(WARDEN_CHAIN)
