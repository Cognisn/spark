"""End to end (A17): a personal SQLite database exported, then imported into PostgreSQL.

The source is a seeded personal-mode SQLite database; the target a PostgreSQL
database at head in testcontainers. Every table a bundle carries is compared
row for row (tests.bundle_compare), with the vectors, the files and the
preferences.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from spark.bundles.importer import ImportTarget, import_bundle
from spark.bundles.reader import Limits, open_bundle
from spark.db import Database
from spark.files.store import MountedVolumeStore
from spark.mode import DatabaseSettings, Dialect
from tests.bundle_compare import compare, compare_vectors
from tests.bundle_seed import PERSONAL_USER, personal_bundle

TARGET = "imported-user-guid"

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("spark_db_settings", ["postgresql"], indirect=True)
async def test_sqlite_export_imported_into_postgresql_row_for_row(
    spark_db: Any, _sqlite_template: Path, tmp_path: Path
) -> None:
    from spark.instance_settings import repository

    data, seeded, source_path = await personal_bundle(_sqlite_template, tmp_path)
    store = MountedVolumeStore(tmp_path / "server-files")
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        report = await import_bundle(
            spark_db,
            bundle,
            ImportTarget(
                user_guid=TARGET,
                store=store,
                quota_bytes=1 << 20,
                mcp_servers=[{"name": "remote-search", "transport": "http"}],
            ),
        )
    source = await Database.open(DatabaseSettings(Dialect.SQLITE, sqlite_path=source_path))
    try:
        compared = await compare(source, PERSONAL_USER, spark_db, TARGET, report.bundle_id)
        vectors = await compare_vectors(source, PERSONAL_USER, spark_db, TARGET)
    finally:
        await source.close()
    # Every table the bundle carries was compared, and each count is what the import reported.
    assert {k: v for k, v in compared.items() if v} == {
        k: v for k, v in report.imported.items() if k != "preferences"
    }
    assert sum(compared.values()) == 36
    assert vectors == 3
    for row in await spark_db.fetch_all(
        "SELECT storage_key, filename FROM user_files WHERE user_guid = ? AND storage_key LIKE ?",
        (TARGET, "%/files/%"),
    ):
        original = row["storage_key"].split("/files/", 1)[1]
        with store.open(TARGET, row["storage_key"]) as stream:
            assert stream.read() == seeded.files[original]
    assert await repository.load_preferences(spark_db, TARGET) == {
        "interface.theme": "light",
        "default_model.model_id": "model-alpha",
    }
    assert len(report.dropped) == 8
