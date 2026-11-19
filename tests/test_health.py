"""``/health`` and ``/ready``: the load balancer's and the container's probes.

``/health`` answers whenever the process serves and depends on nothing. ``/ready``
answers 200 only with the database answering, both Alembic chains at head and the
secrets store readable; its body names each check and never carries a secret,
URL, host name or stack trace. Both need no session, in personal and server mode.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

import spark
from spark import mode
from spark.db import migrate
from spark.mode import DatabaseSettings, Mode, RuntimeSettings
from spark.web.endpoints import health
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_instance_settings_web import MemorySecrets

SECRET_TEXT = "hunter2-dev-only"


class _Ctx:
    def __init__(self, secrets: Any) -> None:
        self.settings = Settings(defaults={"interface": {"session_timeout_minutes": 60}})
        self.secrets = secrets


class _BrokenSecrets(MemorySecrets):
    """A store that cannot be read (the error text carries a secret-looking value)."""

    def has(self, key: str) -> bool:
        raise RuntimeError(f"cannot read the bundle (password={SECRET_TEXT})")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()


@pytest.fixture
def personal_app(sqlite_settings: DatabaseSettings) -> Any:
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    return create_app(_Ctx(MemorySecrets()))  # type: ignore[arg-type]


def _assert_clean(text: str) -> None:
    lowered = text.lower()
    for forbidden in (SECRET_TEXT, "traceback", "postgresql://", "sqlite", ".db", "password"):
        assert forbidden not in lowered


def test_health_needs_no_session_and_no_dependency(personal_app: Any) -> None:
    # No lifespan, so no database is open: liveness must not care.
    client = TestClient(personal_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": spark.__version__}
    assert resp.headers["cache-control"] == "no-store"


def test_ready_is_200_with_every_check_ok(
    personal_app: Any, sqlite_settings: DatabaseSettings
) -> None:
    with app_client(personal_app, sqlite_settings) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ready",
        "version": spark.__version__,
        "checks": {"database": "ok", "schema": "ok", "secrets": "ok"},
    }


def test_ready_is_503_before_the_database_is_open(personal_app: Any) -> None:
    resp = TestClient(personal_app).get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "failed"
    assert body["version"] == spark.__version__


def test_ready_is_503_when_the_schema_is_not_at_head(
    personal_app: Any, sqlite_settings: DatabaseSettings
) -> None:
    with app_client(personal_app, sqlite_settings) as client:
        assert sqlite_settings.sqlite_path is not None
        with sqlite3.connect(sqlite_settings.sqlite_path) as conn:
            conn.execute("UPDATE alembic_version SET version_num = '0001'")
        resp = client.get("/ready")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert checks["schema"] == "failed"
    assert checks["database"] == "ok"
    _assert_clean(resp.text)
    # head() is a real revision: the check failed because the database is behind it.
    assert migrate.head() != "0001"


def test_ready_is_503_when_the_secrets_store_cannot_be_read(
    sqlite_settings: DatabaseSettings,
) -> None:
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    app = create_app(_Ctx(_BrokenSecrets()))  # type: ignore[arg-type]
    with app_client(app, sqlite_settings) as client:
        resp = client.get("/ready")
        health_resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"database": "ok", "schema": "ok", "secrets": "failed"}
    _assert_clean(resp.text)
    assert health_resp.status_code == 200  # liveness is unaffected


def test_a_hung_check_is_a_failed_check(
    personal_app: Any, sqlite_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    release = threading.Event()

    def hang(*_: Any) -> None:
        release.wait(10)

    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(migrate, "check_at_head", hang)
    try:
        with app_client(personal_app, sqlite_settings) as client:
            resp = client.get("/ready")
    finally:
        release.set()
    assert resp.status_code == 503
    assert resp.json()["checks"]["schema"] == "failed"


def test_a_burst_of_probes_costs_one_check(
    personal_app: Any, sqlite_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = migrate.check_at_head

    def counting(settings: DatabaseSettings) -> None:
        calls.append(1)
        real(settings)

    monkeypatch.setattr(migrate, "check_at_head", counting)
    with app_client(personal_app, sqlite_settings) as client:
        started = len(calls)  # start-up checks the schema itself
        for _ in range(5):
            assert client.get("/ready").status_code == 200
        assert len(calls) == started + 1


def test_the_probes_are_unauthenticated_paths() -> None:
    assert health.PROBE_PATHS == {"/health", "/ready"}
