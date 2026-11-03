"""The committed synthetic bundle (format version 1) still opens and imports as committed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spark.bundles import format as bf
from spark.bundles.importer import ImportTarget, import_bundle
from spark.bundles.reader import Limits, open_bundle
from spark.files.store import MountedVolumeStore

FIXTURE = Path(__file__).parent / "fixtures" / "bundles" / "synthetic-v1.zip"


def test_the_committed_bundle_is_version_1_and_passes_every_check() -> None:
    with open_bundle(FIXTURE, Limits()) as bundle:
        assert bundle.manifest["format"] == bf.FORMAT
        assert bundle.manifest["format_version"] == 1
        assert bundle.manifest["counts"]["conversations"] == 2
        assert bundle.manifest["counts"]["autonomous_actions"] == 1
        assert {s for s, _, _ in bundle.skill_files()} == {"note-taker"}


async def test_the_committed_bundle_imports(spark_db: Any, tmp_path: Path) -> None:
    store = MountedVolumeStore(tmp_path / "files")
    with open_bundle(FIXTURE, Limits()) as bundle:
        report = await import_bundle(
            spark_db,
            bundle,
            ImportTarget(user_guid="fixture-user", store=store, quota_bytes=None, mcp_servers=[]),
        )
    assert report.imported["conversations"] == 2
    assert report.imported["messages"] == 3
    assert report.actions_disabled == ["Morning garden check"]
    count = await spark_db.fetch_value(
        "SELECT COUNT(*) FROM messages WHERE user_guid = ?", ("fixture-user",)
    )
    assert count == 3
