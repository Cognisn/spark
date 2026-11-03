"""``spark export``: the personal user's bundle, written to a file."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from spark import launch, mode
from spark.bundles import cli
from spark.bundles.exporter import ExportSources
from spark.bundles.reader import Limits, open_bundle
from spark.files.store import MountedVolumeStore
from spark.mode import DatabaseSettings, Dialect
from tests.bundle_seed import PERSONAL_USER, seed


@pytest.fixture
def personal_db(_sqlite_template: Path, tmp_path: Path) -> DatabaseSettings:
    path = tmp_path / "spark.db"
    shutil.copy(_sqlite_template, path)
    return DatabaseSettings(Dialect.SQLITE, sqlite_path=path)


def _loader(settings: DatabaseSettings, store: Any, server: bool = False) -> Any:
    def load() -> cli.Loaded:
        if server:
            raise mode.ModeConfigError(cli.SERVER_REFUSAL)
        return cli.Loaded(
            database=settings,
            user_guid=PERSONAL_USER,
            sources=ExportSources(
                files=store,
                skills_dir=None,
                preferences={"interface.theme": "light"},
                mcp_servers=[],
            ),
        )

    return load


async def _seed(settings: DatabaseSettings, store: Any) -> None:
    from spark.db import Database

    db = await Database.open(settings)
    try:
        await seed(db, PERSONAL_USER, store)
    finally:
        await db.close()


def test_export_writes_the_bundle_to_the_named_file(
    personal_db: DatabaseSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    store = MountedVolumeStore(tmp_path / "files")
    asyncio.run(_seed(personal_db, store))
    target = tmp_path / "out" / "mine.zip"
    code = cli.run_export(["--output", str(target)], load=_loader(personal_db, store))
    assert code == 0
    with open_bundle(target, Limits()) as bundle:
        assert bundle.manifest["counts"]["conversations"] == 2
        assert bundle.preferences() == {"interface.theme": "light"}
    out = capsys.readouterr().out
    assert str(target) in out and "conversations: 2" in out


def test_export_names_the_file_itself_by_default(
    personal_db: DatabaseSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code = cli.run_export([], load=_loader(personal_db, None))
    assert code == 0
    (written,) = list(tmp_path.glob("spark-bundle-*.zip"))
    with open_bundle(written, Limits()):
        pass


def test_export_never_overwrites_a_file(
    personal_db: DatabaseSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "taken.zip"
    target.write_bytes(b"keep me")
    assert cli.run_export(["--output", str(target)], load=_loader(personal_db, None)) == 2
    assert target.read_bytes() == b"keep me"
    assert "already exists" in capsys.readouterr().err


def test_export_refuses_server_mode(
    personal_db: DatabaseSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.run_export(
        ["--output", str(tmp_path / "x.zip")], load=_loader(personal_db, None, server=True)
    )
    assert code == 2
    assert "personal mode" in capsys.readouterr().err
    assert not (tmp_path / "x.zip").exists()


@pytest.mark.parametrize("args", [["--output"], ["--bogus"], ["a", "b"]])
def test_export_usage(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run_export(args, load=lambda: pytest.fail("must not load")) == 2
    assert "usage: spark export" in capsys.readouterr().err


def test_spark_export_dispatches_to_the_export(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "run_export", lambda args: seen.append(args) or 0)
    with pytest.raises(SystemExit) as exc:
        launch.main(["export", "--output", "b.zip"])
    assert exc.value.code == 0
    assert seen == [["--output", "b.zip"]]
