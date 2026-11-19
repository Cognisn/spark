"""TLS to RDS: ``sslmode=verify-full`` with no ``sslrootcert`` and ``PGSSLROOTCERT`` set.

The AWS deployment (docs/deployment/aws.md) keeps database URLs as
``postgresql://...?sslmode=verify-full`` with no ``sslrootcert`` and sets
``PGSSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem`` in every task, so libpq
finds the authority bundle the image carries. Every connection SPARK opens must
honour that variable, and none may fall back to a weaker mode.

A stand-in server (:class:`tests.tls_helpers.FakePostgresTLS`) answers the TLS
opening with a certificate signed by a test authority. What it records shows what
the client did:

* the right authority in ``PGSSLROOTCERT``: the handshake completes (the variable
  was found and the chain and host name verified);
* a different authority in it: the client refuses the certificate;
* the variable unset or naming a file that is not there: the client refuses, and
  never sends a start-up message without TLS (no fallback to ``require`` or
  ``prefer``).

The same three outcomes are asserted for each way SPARK connects: its pool (the
web server's, and warden's, which borrows it), the worker's pool and its audit
purge's, the worker's lock connection, the settings listener, SPARK's migrations
and warden's.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from spark.db import Database, migrate
from spark.mode import DatabaseSettings, Dialect
from tests.tls_helpers import Authority, FakePostgresTLS, make_authority

Attempt = Callable[[str], Awaitable[None]]


@pytest.fixture(scope="module")
def trusted(tmp_path_factory: pytest.TempPathFactory) -> Authority:
    return make_authority(tmp_path_factory.mktemp("tls-trusted"), "trusted")


@pytest.fixture(scope="module")
def stranger(tmp_path_factory: pytest.TempPathFactory) -> Authority:
    return make_authority(tmp_path_factory.mktemp("tls-stranger"), "stranger")


@pytest.fixture(autouse=True)
def _libpq_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No libpq setting from the host: no default root certificate, no service file."""
    for name in list(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    monkeypatch.setattr(logging.getLogger("psycopg.pool"), "disabled", True)


@pytest.fixture
def server(trusted: Authority) -> Iterator[FakePostgresTLS]:
    with FakePostgresTLS(trusted) as fake:
        yield fake


# -- Every way SPARK connects, each taking a URL and trying once -------------------------


async def _pool(url: str, *, size: tuple[int, int] = (1, 10)) -> None:
    """``Database.open``: the web server's pool, warden's (it borrows it), the worker's."""
    import psycopg_pool

    pools: list[Any] = []

    class Spy(psycopg_pool.AsyncConnectionPool):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            pools.append(self)

    original = psycopg_pool.AsyncConnectionPool
    psycopg_pool.AsyncConnectionPool = Spy  # type: ignore[misc]
    task = asyncio.create_task(
        Database.open(
            DatabaseSettings(Dialect.POSTGRESQL, url=url), min_size=size[0], max_size=size[1]
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.to_thread(_wait_outcome, _CURRENT[0])
    finally:
        task.cancel()
        psycopg_pool.AsyncConnectionPool = original  # type: ignore[misc]
        await asyncio.gather(task, return_exceptions=True)
        for pool in pools:
            await pool.close(timeout=2)


async def _audit_purge_pool(url: str) -> None:
    """The audit purge's own connection (``worker.audit_purge_database_url``)."""
    from spark import worker

    class Settings:
        def get(self, key: str, default: Any = None) -> Any:
            return url if key == worker.AUDIT_PURGE_DATABASE_URL else default

    purge = worker.audit_purge_database(Settings(), None)
    assert purge is not None and purge.url == url
    await _pool(purge.url or "", size=(1, 2))


async def _worker_lock(url: str) -> None:
    """The worker's advisory-lock connection."""
    from spark import worker

    lock = worker.AdvisoryLock(url)
    try:
        assert await lock.try_acquire() is False  # the stand-in never takes a start-up message
    finally:
        await lock.release()


async def _settings_listener(url: str) -> None:
    """The settings listener's dedicated LISTEN connection."""
    from spark.instance_settings import listener

    with pytest.raises(Exception):  # noqa: B017, PT011 - psycopg.OperationalError (any refusal)
        conn = await listener.default_connect(url)
        await conn.close()


async def _spark_migrations(url: str) -> None:
    """``spark migrate``'s and the start-up check's connection (SQLAlchemy over psycopg)."""
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=url)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await asyncio.to_thread(migrate.check_at_head, settings)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await asyncio.to_thread(migrate.upgrade, settings)


async def _warden_migrations(url: str) -> None:
    """warden's own migration and head check (``spark migrate``, start-up and ``/ready``)."""
    from spark.identity import migrations as identity_migrations

    settings = DatabaseSettings(Dialect.POSTGRESQL, url=url)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await asyncio.to_thread(identity_migrations.check_at_head, settings)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await asyncio.to_thread(identity_migrations.upgrade, settings)


ATTEMPTS: dict[str, Attempt] = {
    "pool": _pool,
    "audit_purge": _audit_purge_pool,
    "worker_lock": _worker_lock,
    "settings_listener": _settings_listener,
    "spark_migrations": _spark_migrations,
    "warden_migrations": _warden_migrations,
}

#: The stand-in being exercised, for the helpers that wait on it.
_CURRENT: list[FakePostgresTLS] = []


def _wait_outcome(fake: FakePostgresTLS) -> None:
    """Wait until the stand-in has seen a handshake succeed or fail (or time runs out)."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if fake.handshakes_ok + fake.handshakes_failed >= 1:
            return
        time.sleep(0.05)


async def _run(name: str, fake: FakePostgresTLS, url: str) -> None:
    _CURRENT[:] = [fake]
    await ATTEMPTS[name](url)
    # Attempts that return on their own (a refusal) may be quicker than the stand-in's bookkeeping.
    await asyncio.to_thread(_wait_outcome, fake)


# -- The outcomes ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ATTEMPTS))
async def test_the_bundle_named_by_pgsslrootcert_verifies_the_server(
    name: str, server: FakePostgresTLS, trusted: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSSLROOTCERT", str(trusted.ca_pem))
    await _run(name, server, server.url())
    assert server.handshakes_ok >= 1
    assert server.plaintext_startups == 0


@pytest.mark.parametrize("name", sorted(ATTEMPTS))
async def test_a_different_authority_in_pgsslrootcert_is_refused(
    name: str, server: FakePostgresTLS, stranger: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSSLROOTCERT", str(stranger.ca_pem))
    await _run(name, server, server.url())
    assert server.handshakes_ok == 0
    assert server.handshakes_failed >= 1
    assert server.plaintext_startups == 0


@pytest.mark.parametrize("name", sorted(ATTEMPTS))
async def test_without_pgsslrootcert_nothing_falls_back_to_a_weaker_mode(
    name: str, server: FakePostgresTLS
) -> None:
    await _run(name, server, server.url())
    assert server.handshakes_ok == 0
    assert server.plaintext_startups == 0


@pytest.mark.parametrize("name", sorted(ATTEMPTS))
async def test_a_missing_bundle_file_is_refused_not_ignored(
    name: str, server: FakePostgresTLS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSSLROOTCERT", str(tmp_path / "no-such-bundle.pem"))
    await _run(name, server, server.url())
    assert server.handshakes_ok == 0
    assert server.plaintext_startups == 0


def test_spark_never_adds_or_strips_tls_parameters() -> None:
    """The URL SPARK hands libpq is the one the operator wrote (verify-full stays)."""
    url = "postgresql://spark_web:CHANGEME@db.example.com:5432/spark?sslmode=verify-full"
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=url)
    assert settings.url == url
    engine = migrate._engine(settings)
    try:
        assert engine.url.query == {"sslmode": "verify-full"}
        assert "sslrootcert" not in engine.url.query
    finally:
        engine.dispose()
