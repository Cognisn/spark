"""File storage behind one interface, keyed per user by area-relative keys.

:class:`FileStore` is what the rest of SPARK uses. 0.3.0 ships
:class:`MountedVolumeStore` (a directory, typically a mounted volume in a
container); an S3-compatible store can implement the same protocol later.
Keys are always area-relative and checked with :func:`spark.files.paths.contain`;
a caller never supplies an absolute path.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from spark.files.paths import PathRefused, contain

__all__ = ["FileStore", "MountedVolumeStore", "StoredFile", "UploadTooLarge"]

_STAGING = ".staging"
_CHUNK = 1024 * 1024


class UploadTooLarge(ValueError):
    """The content is larger than the maximum upload size."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"The file is larger than the maximum upload size of {max_bytes:,} bytes.")
        self.max_bytes = max_bytes


@dataclass(frozen=True)
class StoredFile:
    """A file in a user's area."""

    key: str
    size: int


@runtime_checkable
class FileStore(Protocol):
    """Per-user file storage. ``key`` is always relative to the user's area."""

    def put(
        self,
        user_guid: str,
        key: str,
        data: bytes | BinaryIO,
        *,
        max_bytes: int | None = None,
        before_commit: Callable[[int], None] | None = None,
    ) -> StoredFile:
        """Store ``data`` at ``key``, replacing any file there.

        Raises :class:`UploadTooLarge` past ``max_bytes``. ``before_commit`` is
        called with the size once the content is staged and before it becomes
        visible; if it raises, nothing changes (this is where quotas are charged).
        """
        ...

    def open(self, user_guid: str, key: str) -> BinaryIO: ...

    def list(self, user_guid: str, prefix: str = "") -> list[StoredFile]: ...

    def delete(self, user_guid: str, key: str) -> bool: ...

    def usage(self, user_guid: str) -> int: ...


def _chunks(data: bytes | BinaryIO) -> Iterable[bytes]:
    if isinstance(data, bytes):
        yield data
        return
    while chunk := data.read(_CHUNK):
        yield chunk


class MountedVolumeStore:
    """A :class:`FileStore` on a local directory: ``<root>/<area id>/<key>``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def area(self, user_guid: str) -> Path:
        """The user's area directory, created on first use.

        The directory name is a digest of the user GUID, so no GUID can name a
        path of its own.
        """
        if not user_guid:
            raise ValueError("a user is required for a file area")
        digest = hashlib.sha256(user_guid.encode("utf-8")).hexdigest()[:32]
        area = self.root.resolve() / digest
        area.mkdir(parents=True, exist_ok=True)
        return area

    def local_path(self, user_guid: str, key: str) -> Path:
        """The contained local path for ``key`` (for tools that need a real file)."""
        area = self.area(user_guid)
        path = contain(area, key)
        if path != area and path.relative_to(area).parts[0] == _STAGING:
            raise PathRefused("the staging directory is reserved")
        return path

    def _key(self, user_guid: str, path: Path) -> str:
        return path.relative_to(self.area(user_guid)).as_posix()

    def put(
        self,
        user_guid: str,
        key: str,
        data: bytes | BinaryIO,
        *,
        max_bytes: int | None = None,
        before_commit: Callable[[int], None] | None = None,
    ) -> StoredFile:
        target = self.local_path(user_guid, key)
        if target == self.area(user_guid):
            raise PathRefused("a file name is required")
        staging = self.area(user_guid) / _STAGING
        staging.mkdir(exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=staging)
        tmp = Path(tmp_name)
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in _chunks(data):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise UploadTooLarge(max_bytes)
                    out.write(chunk)
            if before_commit is not None:
                before_commit(size)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return StoredFile(self._key(user_guid, target), size)

    def open(self, user_guid: str, key: str) -> BinaryIO:
        path = self.local_path(user_guid, key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.open("rb")

    def list(self, user_guid: str, prefix: str = "") -> list[StoredFile]:
        area = self.area(user_guid)
        base = self.local_path(user_guid, prefix) if prefix else area
        if not base.is_dir():
            return []
        found: list[StoredFile] = []
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(area)
            if rel.parts[0] == _STAGING or not path.is_file() or path.is_symlink():
                continue
            found.append(StoredFile(rel.as_posix(), path.stat().st_size))
        return found

    def delete(self, user_guid: str, key: str) -> bool:
        path = self.local_path(user_guid, key)
        if path.is_dir() and path != self.area(user_guid):
            shutil.rmtree(path)
            return True
        if path.is_file():
            path.unlink()
            return True
        return False

    def usage(self, user_guid: str) -> int:
        return sum(f.size for f in self.list(user_guid))
