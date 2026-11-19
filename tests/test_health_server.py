"""``/health`` and ``/ready`` in server mode: warden attached, no session, PostgreSQL.

The probes sit outside warden's checks: a request with no cookie and no bearer
token still gets its answer, while the rest of the site still refuses it.
"""

from __future__ import annotations

import psycopg
import pytest

import spark
from spark.mode import DatabaseSettings
from tests.identity_screen_helpers import server_db  # noqa: F401  (fixture)
from tests.identity_web_helpers import server_client, write_config

pytestmark = pytest.mark.postgres


@pytest.fixture
def config(tmp_path):  # type: ignore[no-untyped-def]
    return write_config(tmp_path / "config.yaml")


def test_health_and_ready_need_no_session(config, server_db: DatabaseSettings) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    with server_client(config, server_db) as client:
        assert (
            client.get(
                "/conversations/api/list", headers={"Accept": "application/json"}
            ).status_code
            == 401
        )
        health = client.get("/health")
        ready = client.get("/ready")
    assert health.status_code == 200
    assert health.json()["version"] == spark.__version__
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "schema": "ok", "secrets": "ok"}


def test_ready_is_503_when_warden_is_not_at_head(config, server_db: DatabaseSettings) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    with server_client(config, server_db) as client:
        assert server_db.url is not None
        with psycopg.connect(server_db.url, autocommit=True) as conn:
            conn.execute("UPDATE warden.alembic_version SET version_num = 'unknown_revision'")
        resp = client.get("/ready")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert checks["schema"] == "failed"
    assert checks["database"] == "ok"
    assert "unknown_revision" not in resp.text
    assert "postgresql://" not in resp.text


def test_the_body_never_names_the_database(config, server_db: DatabaseSettings) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    with server_client(config, server_db) as client:
        text = client.get("/ready").text
    assert server_db.url is not None
    host = server_db.url.split("@", 1)[-1]
    assert host not in text
