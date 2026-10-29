"""The FileStore protocol and its mounted-volume implementation."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from spark.files.paths import PathRefused
from spark.files.store import FileStore, MountedVolumeStore, UploadTooLarge

ALICE = "user-alice-0001"
BOB = "user-bob-0002"


@pytest.fixture
def store(tmp_path: Path) -> MountedVolumeStore:
    return MountedVolumeStore(tmp_path / "files")


def test_mounted_volume_is_a_file_store(store: MountedVolumeStore) -> None:
    assert isinstance(store, FileStore)


class TestPutOpen:
    def test_put_bytes_then_open(self, store: MountedVolumeStore) -> None:
        stored = store.put(ALICE, "notes/a.txt", b"synthetic")
        assert stored.key == "notes/a.txt"
        assert stored.size == 9
        with store.open(ALICE, "notes/a.txt") as fh:
            assert fh.read() == b"synthetic"

    def test_put_stream(self, store: MountedVolumeStore) -> None:
        stored = store.put(ALICE, "b.bin", io.BytesIO(b"x" * 100_000))
        assert stored.size == 100_000

    def test_key_is_normalised(self, store: MountedVolumeStore) -> None:
        assert store.put(ALICE, "a\\b\\.\\c.txt", b"1").key == "a/b/c.txt"

    def test_put_replaces_atomically(self, store: MountedVolumeStore) -> None:
        store.put(ALICE, "a.txt", b"old")
        store.put(ALICE, "a.txt", b"newer")
        with store.open(ALICE, "a.txt") as fh:
            assert fh.read() == b"newer"

    def test_open_missing_raises(self, store: MountedVolumeStore) -> None:
        with pytest.raises(FileNotFoundError):
            store.open(ALICE, "missing.txt")

    @pytest.mark.parametrize("key", ["../x", "/etc/passwd", "C:\\x", "a:b", ""])
    def test_escaping_keys_are_refused(self, store: MountedVolumeStore, key: str) -> None:
        with pytest.raises(PathRefused):
            store.put(ALICE, key, b"x")
        with pytest.raises(PathRefused):
            store.open(ALICE, key)
        with pytest.raises(PathRefused):
            store.delete(ALICE, key)

    def test_reserved_staging_directory_is_refused(self, store: MountedVolumeStore) -> None:
        with pytest.raises(PathRefused):
            store.put(ALICE, ".staging/x", b"x")


class TestLimit:
    def test_upload_over_the_limit_is_refused_and_leaves_nothing(
        self, store: MountedVolumeStore
    ) -> None:
        with pytest.raises(UploadTooLarge):
            store.put(ALICE, "big.bin", io.BytesIO(b"x" * 1025), max_bytes=1024)
        assert store.list(ALICE) == []
        assert store.usage(ALICE) == 0
        assert [p for p in store.area(ALICE).rglob("*") if p.is_file()] == []

    def test_upload_at_the_limit_is_accepted(self, store: MountedVolumeStore) -> None:
        assert store.put(ALICE, "ok.bin", b"x" * 1024, max_bytes=1024).size == 1024

    def test_refused_replacement_keeps_the_old_file(self, store: MountedVolumeStore) -> None:
        store.put(ALICE, "a.txt", b"old")
        with pytest.raises(UploadTooLarge):
            store.put(ALICE, "a.txt", b"x" * 10, max_bytes=5)
        with store.open(ALICE, "a.txt") as fh:
            assert fh.read() == b"old"


class TestBeforeCommit:
    def test_hook_sees_the_size_before_the_file_appears(self, store: MountedVolumeStore) -> None:
        seen: list[tuple[int, bool]] = []

        def hook(size: int) -> None:
            seen.append((size, (store.area(ALICE) / "a.txt").exists()))

        store.put(ALICE, "a.txt", b"12345", before_commit=hook)
        assert seen == [(5, False)]

    def test_a_refusing_hook_leaves_the_old_file_and_no_staging(
        self, store: MountedVolumeStore
    ) -> None:
        store.put(ALICE, "a.txt", b"old")

        def refuse(size: int) -> None:
            raise RuntimeError("over quota")

        with pytest.raises(RuntimeError):
            store.put(ALICE, "a.txt", b"new content", before_commit=refuse)
        with store.open(ALICE, "a.txt") as fh:
            assert fh.read() == b"old"
        assert [f.key for f in store.list(ALICE)] == ["a.txt"]
        assert list((store.area(ALICE) / ".staging").iterdir()) == []


class TestPerUser:
    def test_areas_are_separate(self, store: MountedVolumeStore) -> None:
        store.put(ALICE, "a.txt", b"alice")
        with pytest.raises(FileNotFoundError):
            store.open(BOB, "a.txt")
        assert store.list(BOB) == []

    def test_area_directory_is_not_derived_from_the_raw_guid(
        self, store: MountedVolumeStore
    ) -> None:
        area = store.area("../../escape")
        assert area.parent == store.root.resolve()
        assert ".." not in area.name

    def test_empty_user_is_refused(self, store: MountedVolumeStore) -> None:
        with pytest.raises(ValueError):
            store.area("")


class TestListDeleteUsage:
    def test_list_and_usage(self, store: MountedVolumeStore) -> None:
        store.put(ALICE, "a.txt", b"12345")
        store.put(ALICE, "dir/b.txt", b"123")
        assert sorted(f.key for f in store.list(ALICE)) == ["a.txt", "dir/b.txt"]
        assert [f.key for f in store.list(ALICE, "dir")] == ["dir/b.txt"]
        assert store.usage(ALICE) == 8

    def test_delete(self, store: MountedVolumeStore) -> None:
        store.put(ALICE, "a.txt", b"12345")
        assert store.delete(ALICE, "a.txt") is True
        assert store.delete(ALICE, "a.txt") is False
        assert store.usage(ALICE) == 0

    def test_local_path_is_contained(self, store: MountedVolumeStore) -> None:
        path = store.local_path(ALICE, "x/y.txt")
        assert path.is_relative_to(store.area(ALICE))
        with pytest.raises(PathRefused):
            store.local_path(ALICE, "../y.txt")
