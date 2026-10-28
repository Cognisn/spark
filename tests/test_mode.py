"""Tests for the deployment mode setting and its start-up validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spark import mode
from spark.mode import Dialect, Mode, ModeConfigError


class _Settings:
    """Minimal stand-in for konfig settings: dotted-key ``get`` over a nested dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


SQLITE_PATH = Path("data") / "spark.db"


def _resolve(data: dict[str, Any]) -> mode.RuntimeSettings:
    return mode.resolve(_Settings(data), default_sqlite_path=SQLITE_PATH)


@pytest.fixture(autouse=True)
def _reset_mode():
    yield
    mode.reset()


class TestPersonalMode:
    def test_defaults_to_personal_with_sqlite(self):
        rs = _resolve({})
        assert rs.mode is Mode.PERSONAL
        assert rs.database.dialect is Dialect.SQLITE
        assert rs.database.sqlite_path == SQLITE_PATH
        assert rs.database.url is None

    def test_explicit_personal(self):
        assert _resolve({"mode": "personal"}).mode is Mode.PERSONAL

    def test_mode_is_case_insensitive(self):
        assert _resolve({"mode": " Personal "}).mode is Mode.PERSONAL

    def test_legacy_sqlite_type_is_accepted(self):
        rs = _resolve({"database": {"type": "sqlite", "path": "spark.db"}})
        assert rs.database.dialect is Dialect.SQLITE

    @pytest.mark.parametrize("db_type", ["mysql", "mariadb", "mssql"])
    def test_removed_backends_are_refused(self, db_type):
        with pytest.raises(ModeConfigError, match="no longer supported"):
            _resolve({"database": {"type": db_type}})

    def test_legacy_postgresql_type_points_at_server_mode(self):
        with pytest.raises(ModeConfigError, match="server mode"):
            _resolve({"database": {"type": "postgresql"}})

    def test_database_url_is_refused_in_personal_mode(self):
        with pytest.raises(ModeConfigError, match="server mode"):
            _resolve({"database": {"url": "postgresql://spark@db.example.com/spark"}})


class TestServerMode:
    def test_requires_a_database_url(self):
        with pytest.raises(ModeConfigError, match=r"requires database\.url \(a PostgreSQL URL\)"):
            _resolve({"mode": "server"})

    def test_blank_url_is_missing(self):
        with pytest.raises(ModeConfigError, match=r"requires database\.url \(a PostgreSQL URL\)"):
            _resolve({"mode": "server", "database": {"url": "   "}})

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///spark.db",
            "sqlite+aiosqlite:///spark.db",
            "spark.db",
            "mysql://spark@db.example.com/spark",
        ],
    )
    def test_refuses_anything_but_postgresql(self, url):
        with pytest.raises(ModeConfigError, match="PostgreSQL"):
            _resolve({"mode": "server", "database": {"url": url}})

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://spark:example-dev-only@db.example.com:5432/spark",
            "postgres://spark@192.0.2.10/spark",
            "postgresql+psycopg://spark@db.example.org/spark",
        ],
    )
    def test_accepts_postgresql_urls(self, url):
        rs = _resolve({"mode": "server", "database": {"url": url}})
        assert rs.mode is Mode.SERVER
        assert rs.database.dialect is Dialect.POSTGRESQL
        assert rs.database.sqlite_path is None

    def test_driver_suffix_is_normalised_for_psycopg(self):
        rs = _resolve(
            {
                "mode": "server",
                "database": {"url": "postgresql+psycopg://spark@db.example.org/spark"},
            }
        )
        assert rs.database.url == "postgresql://spark@db.example.org/spark"

    def test_legacy_sqlite_type_default_does_not_block_server_mode(self):
        rs = _resolve(
            {
                "mode": "server",
                "database": {"type": "sqlite", "url": "postgresql://spark@db.example.com/spark"},
            }
        )
        assert rs.mode is Mode.SERVER


class TestInvalidMode:
    @pytest.mark.parametrize("value", ["", "multi", "team", 3])
    def test_unknown_mode_is_refused(self, value):
        with pytest.raises(ModeConfigError, match="mode must be"):
            _resolve({"mode": value})


class TestAccessor:
    def test_current_mode_before_configure_raises(self):
        with pytest.raises(RuntimeError, match="not been configured"):
            mode.current_mode()

    def test_configure_sets_the_accessor(self):
        rs = _resolve({"mode": "server", "database": {"url": "postgresql://db.example.com/spark"}})
        mode.configure(rs)
        assert mode.current_mode() is Mode.SERVER
        assert mode.is_server() is True
        assert mode.current() is rs

    def test_personal_accessor(self):
        mode.configure(_resolve({}))
        assert mode.current_mode() is Mode.PERSONAL
        assert mode.is_server() is False

    def test_reset_clears_the_accessor(self):
        mode.configure(_resolve({}))
        mode.reset()
        with pytest.raises(RuntimeError):
            mode.current_mode()


class TestErrorMessagesDoNotLeakSecrets:
    def test_password_is_not_echoed(self):
        with pytest.raises(ModeConfigError) as exc:
            _resolve({"mode": "server", "database": {"url": "mysql://spark:s3cret-dev-only@x/y"}})
        assert "s3cret" not in str(exc.value)
