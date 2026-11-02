"""Tests for the ``spark migrate`` command."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from spark import launch
from spark.core import application
from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings


class TestLaunch:
    def test_migrate_subcommand_runs_migrations(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(application, "run_migrate", lambda: calls.append("migrate") or 0)
        monkeypatch.setattr(application, "run", lambda: calls.append("serve"))
        with pytest.raises(SystemExit) as exc:
            launch.main(["migrate"])
        assert exc.value.code == 0
        assert calls == ["migrate"]

    def test_no_arguments_starts_the_server(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(application, "run", lambda: calls.append("serve"))
        launch.main([])
        assert calls == ["serve"]

    def test_unknown_subcommand_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(application, "run", lambda: pytest.fail("must not start"))
        with pytest.raises(SystemExit) as exc:
            launch.main(["frobnicate"])
        assert exc.value.code == 2
        assert "frobnicate" in capsys.readouterr().err


@pytest.fixture
def _isolated_chains() -> Iterator[None]:
    saved = migrate.registered_chains()
    migrate.clear_chains()
    yield
    migrate.clear_chains()
    for chain in saved:
        migrate.register_chain(chain)


@pytest.mark.usefixtures("_isolated_chains")
class TestRunMigrations:
    def test_migrates_the_configured_database(self, tmp_path: Path):
        runtime = RuntimeSettings(
            Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
        )
        # warden's chain is PostgreSQL only: skipped in personal mode.
        assert application.run_migrations(runtime) == {"spark": migrate.head(), "warden": None}
        migrate.check_at_head(runtime.database)
