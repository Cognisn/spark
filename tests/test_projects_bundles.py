"""User bundles, format version 2 (PC15): projects travel with a personal export and import
into a server account, owned by the target user and shared with nobody; version 1 bundles
still import; a version 1 bundle carrying projects is refused. Runs on SQLite and (marked
postgres) PostgreSQL."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from spark.bundles import format as bf
from spark.bundles.importer import ImportTarget, import_bundle
from spark.bundles.reader import BundleRefused, Limits, open_bundle
from spark.database import projects as projects_db
from spark.files.store import MountedVolumeStore
from spark.projects.access import ProjectRole, project_access

FIXTURES = Path(__file__).parent / "fixtures" / "bundles"
TARGET = "bundle-target-user"


async def _import(db: Any, source: Path | io.BytesIO, tmp_path: Path) -> Any:
    store = MountedVolumeStore(tmp_path / "files")
    with open_bundle(source, Limits()) as bundle:
        return await import_bundle(
            db,
            bundle,
            ImportTarget(user_guid=TARGET, store=store, quota_bytes=None, mcp_servers=[]),
        )


def test_the_committed_version_2_bundle_carries_projects() -> None:
    with open_bundle(FIXTURES / "synthetic-v2.zip", Limits()) as bundle:
        assert bundle.manifest["format_version"] == 2
        assert bundle.manifest["counts"]["projects"] == 2
        assert bundle.manifest["counts"]["project_resources"] == 1
        names = [r["name"] for r in bundle.rows("projects")]
        assert names == ["Garden", "Shade bed"]


async def test_the_committed_version_2_bundle_imports_its_projects(
    spark_db: Any, tmp_path: Path
) -> None:
    report = await _import(spark_db, FIXTURES / "synthetic-v2.zip", tmp_path)
    assert report.projects == ["Garden", "Garden > Shade bed"]
    assert report.summary()["projects"] == ["Garden", "Garden > Shade bed"]
    assert report.imported["projects"] == 2
    assert report.imported["project_resources"] == 1
    garden, shade = (report.ids["projects"][1], report.ids["projects"][2])
    for pid, depth in ((garden, 1), (shade, 2)):
        access = await project_access(spark_db, TARGET, pid)
        assert access is not None and access.role is ProjectRole.OWNER
        assert access.depth == depth and access.root_id == garden
    assert await spark_db.fetch_value("SELECT COUNT(*) FROM project_members") == 0
    chat = report.ids["conversations"][min(report.ids["conversations"])]
    placed = await spark_db.fetch_value(
        "SELECT project_id FROM conversations WHERE id = ?", (chat,)
    )
    assert placed == shade
    resources = await projects_db.list_resources(spark_db, TARGET, garden)
    assert [(r["filename"], r["added_by"]) for r in resources] == [("notes.txt", TARGET)]
    context = await projects_db.conversation_resources(spark_db, TARGET, chat)
    assert [lv.instructions for lv in context] == ["Use metric units.", "Prefer perennials."]


async def test_version_1_still_imports_without_projects(spark_db: Any, tmp_path: Path) -> None:
    report = await _import(spark_db, FIXTURES / "synthetic-v1.zip", tmp_path)
    assert report.format_version == 1 and report.projects == []
    assert await spark_db.fetch_value("SELECT COUNT(*) FROM projects") == 0


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _rebuild(members: dict[str, bytes], version: int) -> io.BytesIO:
    import hashlib

    doc = json.loads(members[bf.MANIFEST])
    doc["format_version"] = version
    doc["members"] = {
        n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
        for n, b in members.items()
        if n != bf.MANIFEST
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in members.items():
            if name != bf.MANIFEST:
                zf.writestr(name, body)
        zf.writestr(bf.MANIFEST, json.dumps(doc))
    out.seek(0)
    return out


def test_a_version_1_bundle_with_project_tables_is_refused() -> None:
    members = _members(FIXTURES / "synthetic-v2.zip")
    with pytest.raises(BundleRefused, match="format version 1, which has no project"):
        open_bundle(_rebuild(members, 1), Limits())


def test_a_version_1_conversation_with_a_project_is_refused() -> None:
    members = {
        n: b
        for n, b in _members(FIXTURES / "synthetic-v2.zip").items()
        if n not in ("data/projects.jsonl", "data/project_resources.jsonl")
    }
    with open_bundle(_rebuild(members, 1), Limits()) as bundle:
        with pytest.raises(BundleRefused, match="column the format does not allow"):
            list(bundle.rows("conversations"))


async def test_a_project_below_the_fifth_level_is_skipped(spark_db: Any, tmp_path: Path) -> None:
    buf = io.BytesIO()
    with bf.BundleWriter(buf, spark_version="x") as writer:
        writer.add_rows(
            "projects",
            [
                {"id": i, "parent_id": i - 1 if i > 1 else None, "name": f"L{i}"}
                for i in range(1, 8)
            ],
        )
    buf.seek(0)
    report = await _import(spark_db, buf, tmp_path)
    assert report.imported["projects"] == 5
    assert any("below the fifth level" in s for s in report.skipped)
    assert await spark_db.fetch_value("SELECT MAX(depth) FROM projects") == 5
