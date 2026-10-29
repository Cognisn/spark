"""The file area service: limits from settings, uploads and created files under quota."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from spark import files as files_pkg
from spark.database import files as files_db
from spark.files.area import FileArea, FileAreaNotConfigured, FileLimits
from spark.files.store import MountedVolumeStore, UploadTooLarge
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

ALICE = "user-alice-0001"
BOB = "user-bob-0002"
MB = 1024 * 1024


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self._values.get(key, default)


class TestLimits:
    def test_defaults_personal(self) -> None:
        limits = FileLimits.from_settings(_Settings({}), server=False)
        assert limits.max_upload_bytes == 50 * MB
        assert limits.quota_bytes is None

    def test_defaults_server(self) -> None:
        limits = FileLimits.from_settings(_Settings({}), server=True)
        assert limits.max_upload_bytes == 50 * MB
        assert limits.quota_bytes == 1024 * MB

    def test_configured_values(self) -> None:
        limits = FileLimits.from_settings(
            _Settings({"files.max_upload_mb": 2, "files.quota_mb": "0.5"}), server=True
        )
        assert limits.max_upload_bytes == 2 * MB
        assert limits.quota_bytes == MB // 2

    def test_zero_means_no_limit(self) -> None:
        limits = FileLimits.from_settings(
            _Settings({"files.max_upload_mb": 0, "files.quota_mb": 0}), server=True
        )
        assert limits.max_upload_bytes is None
        assert limits.quota_bytes is None

    @pytest.mark.parametrize("bad", ["lots", -1, None, [], True])
    def test_invalid_values_fall_back_to_the_default(self, bad: Any) -> None:
        limits = FileLimits.from_settings(_Settings({"files.max_upload_mb": bad}), server=True)
        assert limits.max_upload_bytes == 50 * MB


class TestConfiguration:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Iterator[None]:
        yield
        files_pkg.reset()

    def test_personal_root_defaults_under_the_data_directory(self, tmp_path: Path) -> None:
        files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
        assert files_pkg.file_store().root == tmp_path / "files"

    def test_configured_root(self, tmp_path: Path) -> None:
        files_pkg.configure(
            _Settings({"files.root": str(tmp_path / "vol")}),
            data_dir=tmp_path,
            runtime_mode=Mode.SERVER,
        )
        assert files_pkg.file_store().root == tmp_path / "vol"

    def test_server_mode_needs_a_configured_root(self, tmp_path: Path) -> None:
        files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.SERVER)
        with pytest.raises(FileAreaNotConfigured):
            files_pkg.file_store()

    def test_unconfigured_process_has_no_store(self) -> None:
        files_pkg.reset()
        with pytest.raises(FileAreaNotConfigured):
            files_pkg.file_store()

    def test_limits_are_read_at_each_call(self, tmp_path: Path) -> None:
        values: dict[str, Any] = {"files.quota_mb": 1}
        files_pkg.configure(_Settings(values), data_dir=tmp_path, runtime_mode=Mode.SERVER)
        assert files_pkg.file_limits().quota_bytes == MB
        values["files.quota_mb"] = 2
        assert files_pkg.file_limits().quota_bytes == 2 * MB

    def test_configure_runtime_configures_the_file_area(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark import mode
        from spark.core import application

        monkeypatch.setattr(application, "_get_data_path", lambda: tmp_path)
        try:
            application.configure_runtime(_Settings({"mode": "personal"}))
            assert files_pkg.file_store().root == tmp_path / "files"
        finally:
            mode.reset()


@pytest.fixture
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


def _area(tmp_path: Path, bridge: Any, *, quota: int | None, max_upload: int | None) -> FileArea:
    return FileArea(
        MountedVolumeStore(tmp_path / "files"),
        bridge,
        lambda: FileLimits(max_upload_bytes=max_upload, quota_bytes=quota),
    )


class TestSave:
    def test_save_records_and_stores(self, tmp_path: Path, bridge: Any) -> None:
        area = _area(tmp_path, bridge, quota=None, max_upload=None)
        fid = area.save(
            ALICE, "uploads/1/a.txt", b"hello", filename="a.txt", mime_type="text/plain"
        )
        row = bridge.call(files_db.get_user_file, ALICE, fid)
        assert row["storage_key"] == "uploads/1/a.txt"
        assert row["size_bytes"] == 5
        with area.store.open(ALICE, "uploads/1/a.txt") as fh:
            assert fh.read() == b"hello"

    def test_over_the_upload_limit_is_refused_and_not_recorded(
        self, tmp_path: Path, bridge: Any
    ) -> None:
        area = _area(tmp_path, bridge, quota=None, max_upload=4)
        with pytest.raises(UploadTooLarge):
            area.save(ALICE, "a.txt", io.BytesIO(b"hello"), filename="a.txt")
        assert bridge.call(files_db.list_user_files, ALICE) == []
        assert area.store.list(ALICE) == []

    def test_over_quota_is_refused_and_nothing_is_stored(self, tmp_path: Path, bridge: Any) -> None:
        area = _area(tmp_path, bridge, quota=8, max_upload=None)
        area.save(ALICE, "a.txt", b"12345", filename="a.txt")
        with pytest.raises(files_db.QuotaExceeded):
            area.save(ALICE, "b.txt", b"12345", filename="b.txt")
        assert [f.key for f in area.store.list(ALICE)] == ["a.txt"]
        assert bridge.call(files_db.get_usage, ALICE) == 5

    def test_parallel_uploads_against_one_quota(self, tmp_path: Path, bridge: Any) -> None:
        """Sixteen threads upload 10 bytes each at once against a 55-byte quota."""
        area = _area(tmp_path, bridge, quota=55, max_upload=None)
        start = threading.Barrier(16)

        def upload(i: int) -> bool:
            start.wait()
            try:
                area.save(ALICE, f"p/{i}.bin", io.BytesIO(b"x" * 10), filename=f"{i}.bin")
            except files_db.QuotaExceeded:
                return False
            return True

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(upload, range(16)))
        assert sum(results) == 5
        assert bridge.call(files_db.get_usage, ALICE) == 50
        assert area.store.usage(ALICE) == 50
        assert len(bridge.call(files_db.list_user_files, ALICE)) == 5


class TestCreate:
    def test_create_writes_through_staging_and_records(self, tmp_path: Path, bridge: Any) -> None:
        area = _area(tmp_path, bridge, quota=None, max_upload=None)

        def writer(path: Path) -> None:
            assert path.suffix == ".docx"
            assert ".staging" in path.parts
            path.write_bytes(b"synthetic document")

        final = area.create(ALICE, "reports/q3.docx", writer, conversation_id=7)
        assert final == area.store.local_path(ALICE, "reports/q3.docx")
        row = bridge.call(files_db.get_user_file_by_key, ALICE, "reports/q3.docx")
        assert row["source"] == "created"
        assert row["conversation_id"] == 7
        assert row["size_bytes"] == 18

    def test_create_over_quota_leaves_nothing(self, tmp_path: Path, bridge: Any) -> None:
        area = _area(tmp_path, bridge, quota=4, max_upload=None)
        with pytest.raises(files_db.QuotaExceeded):
            area.create(ALICE, "big.pdf", lambda p: p.write_bytes(b"12345"))
        assert area.store.list(ALICE) == []
        assert [p for p in area.store.area(ALICE).rglob("*") if p.is_file()] == []

    def test_create_writer_failure_leaves_nothing(self, tmp_path: Path, bridge: Any) -> None:
        area = _area(tmp_path, bridge, quota=None, max_upload=None)

        def broken(path: Path) -> None:
            path.write_bytes(b"partial")
            raise RuntimeError("library failure")

        with pytest.raises(RuntimeError):
            area.create(ALICE, "x.xlsx", broken)
        assert area.store.list(ALICE) == []
        assert bridge.call(files_db.list_user_files, ALICE) == []


class TestCreateMany:
    def test_a_target_outside_the_area_is_refused(self, tmp_path: Path, bridge: Any) -> None:
        area = _area(tmp_path, bridge, quota=None, max_upload=None)
        outside = tmp_path / "outside.txt"
        calls: list[Path] = []
        with pytest.raises(ValueError):
            area.create_many(ALICE, [(outside, calls.append)])
        with pytest.raises(ValueError):
            area.create_many(BOB, [(area.store.local_path(ALICE, "a.txt"), calls.append)])
        assert calls == [], "the writer ran for a target outside the area"
        assert not outside.exists()
