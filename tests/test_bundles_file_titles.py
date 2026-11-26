"""User bundles carry each file's title and kind (question 90, wave 29).

A chart, a diagram, a renamed document and an attachment export and import with their
titles and kinds, on SQLite and (marked postgres) PostgreSQL. Version 1 bundles and
version 2 bundles written before the two columns were carried still import with the
defaults (the file name, and the kind from the file's source). Synthetic data only.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from spark.bundles import format as bf
from spark.bundles.exporter import ExportSources, export_bundle
from spark.bundles.importer import ImportTarget, _bundle_kind, _bundle_title, import_bundle
from spark.bundles.reader import BundleRefused, Limits, open_bundle
from spark.database import files as files_db
from spark.files.store import MountedVolumeStore

FIXTURES = Path(__file__).parent / "fixtures" / "bundles"
SOURCE = "titles-source-user"
TARGET = "titles-target-user"

# (key, content, source, title, kind)
FILES = [
    ("created/sales.chart.json", b'{"chart": 1}', "created", "Quarterly sales", "chart"),
    ("created/flow.mmd", b"graph TD; A-->B", "created", "Sign-in flow", "diagram"),
    ("created/notes.md", b"# Notes", "created", "Shade bed planting notes", "document"),
    ("uploads/ab12/plan.pdf", b"%PDF-1.4 synthetic", "upload", "Garden plan", "attachment"),
]


async def _export(db: Any, store: MountedVolumeStore) -> bytes:
    new = []
    for key, data, source, title, kind in FILES:
        store.put(SOURCE, key, data)
        new.append(
            files_db.NewUserFile(
                key=key,
                filename=key.rsplit("/", 1)[1],
                size=len(data),
                source=source,
                title=title,
                kind=kind,
            )
        )
    await files_db.record_user_files(db, SOURCE, new, quota_bytes=None)
    out = io.BytesIO()
    await export_bundle(
        db,
        SOURCE,
        out,
        ExportSources(files=store, skills_dir=None, preferences={}, mcp_servers=[]),
        spark_version="test",
    )
    return out.getvalue()


async def _import(db: Any, data: bytes | Path, store: MountedVolumeStore) -> Any:
    source: Any = io.BytesIO(data) if isinstance(data, bytes) else data
    with open_bundle(source, Limits()) as bundle:
        return await import_bundle(
            db,
            bundle,
            ImportTarget(user_guid=TARGET, store=store, quota_bytes=None, mcp_servers=[]),
        )


async def _imported(db: Any) -> dict[str, tuple[str, str]]:
    rows = await db.fetch_all(
        "SELECT filename, title, kind FROM user_files WHERE user_guid = ? AND storage_key LIKE ?",
        (TARGET, "%/files/%"),
    )
    return {r["filename"]: (r["title"], r["kind"]) for r in rows}


def _rewrite(data: bytes, *, version: int, drop_columns: bool) -> bytes:
    """The bundle as an older Spark wrote it: another format version, and (when asked)
    no title or kind on its file rows. Sizes and digests are recomputed."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    if drop_columns:
        rows = [json.loads(x) for x in members["data/user_files.jsonl"].decode().splitlines()]
        for row in rows:
            row.pop("title")
            row.pop("kind")
        members["data/user_files.jsonl"] = "".join(json.dumps(r) + "\n" for r in rows).encode()
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
    return out.getvalue()


async def test_titles_and_kinds_round_trip(spark_db: Any, tmp_path: Path) -> None:
    store = MountedVolumeStore(tmp_path / "files")
    data = await _export(spark_db, store)
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        carried = {r["filename"]: (r["title"], r["kind"]) for r in bundle.rows("user_files")}
    expected = {key.rsplit("/", 1)[1]: (title, kind) for key, _, _, title, kind in FILES}
    assert carried == expected
    await _import(spark_db, data, store)
    assert await _imported(spark_db) == expected


@pytest.mark.parametrize("version", [1, 2])
async def test_a_bundle_without_titles_imports_with_the_defaults(
    spark_db: Any, tmp_path: Path, version: int
) -> None:
    store = MountedVolumeStore(tmp_path / "files")
    data = await _export(spark_db, store)
    older = _rewrite(data, version=version, drop_columns=True)
    report = await _import(spark_db, older, store)
    assert report.format_version == version
    assert await _imported(spark_db) == {
        "sales.chart.json": ("sales.chart.json", "document"),
        "flow.mmd": ("flow.mmd", "document"),
        "notes.md": ("notes.md", "document"),
        "plan.pdf": ("plan.pdf", "attachment"),
    }


async def test_the_committed_bundles_import_with_the_defaults(
    spark_db: Any, tmp_path: Path
) -> None:
    store = MountedVolumeStore(tmp_path / "files")
    for name in ("synthetic-v1.zip", "synthetic-v2.zip"):
        with open_bundle(FIXTURES / name, Limits()) as bundle:
            for row in bundle.rows("user_files"):
                assert "title" not in row and "kind" not in row
        await spark_db.execute("DELETE FROM user_files")
        await _import(spark_db, FIXTURES / name, store)
        rows = await spark_db.fetch_all(
            "SELECT filename, title, kind, source FROM user_files WHERE storage_key LIKE ?",
            ("%/files/%",),
        )
        assert rows
        for row in rows:
            assert row["title"] == row["filename"]
            assert row["kind"] == ("attachment" if row["source"] == "upload" else "document")


async def test_a_title_and_a_kind_a_bundle_gets_wrong_fall_back(
    spark_db: Any, tmp_path: Path
) -> None:
    store = MountedVolumeStore(tmp_path / "files")
    buf = io.BytesIO()
    with bf.BundleWriter(buf, spark_version="x") as writer:
        blob = writer.add_blob(b"synthetic")
        rows: list[tuple[str, dict[str, Any]]] = [
            ("a.txt", {"title": "<b>Bold</b>\nTitle", "kind": "chart"}),
            ("b.txt", {"title": "  ", "kind": "malware"}),
            ("c.txt", {"title": 7, "kind": ["chart"]}),
            ("d.txt", {"title": "x" * 500, "kind": None}),
            ("e.txt", {}),
        ]
        writer.add_rows(
            "user_files",
            [
                {
                    "id": i,
                    "storage_key": name,
                    "filename": name,
                    "size_bytes": 9,
                    "source": "created",
                    "blob": blob,
                    **extra,
                }
                for i, (name, extra) in enumerate(rows, 1)
            ],
        )
    await _import(spark_db, buf.getvalue(), store)
    got = await _imported(spark_db)
    assert got["a.txt"] == ("＜b＞Bold＜/b＞ Title", "chart")
    assert got["b.txt"] == ("b.txt", "document")
    assert got["c.txt"] == ("c.txt", "document")
    assert got["d.txt"] == ("x" * 120, "document")
    assert got["e.txt"] == ("e.txt", "document")


def test_the_helpers_validate_kind_and_title() -> None:
    for kind in ("document", "chart", "diagram", "preview", "attachment"):
        assert _bundle_kind(kind) == kind
    for bad in ("Chart", "", "x", None, 3, ["chart"]):
        assert _bundle_kind(bad) is None
    assert _bundle_title("Plain") == "Plain"
    assert _bundle_title(" \n ") is None and _bundle_title(None) is None
    assert _bundle_title(5) is None


async def test_a_version_1_bundle_carrying_titles_is_refused(spark_db: Any, tmp_path: Path) -> None:
    store = MountedVolumeStore(tmp_path / "files")
    data = await _export(spark_db, store)
    claimed = _rewrite(data, version=1, drop_columns=False)
    with open_bundle(io.BytesIO(claimed), Limits()) as bundle:
        with pytest.raises(BundleRefused, match="column the format does not allow"):
            list(bundle.rows("user_files"))
