"""The file area service: storage, limits, quota and metadata together.

:class:`FileArea` is synchronous: endpoints run it with ``asyncio.to_thread``
and tools already run on worker threads. Metadata and quota go through the
bridge (:class:`spark.db.SyncDatabase`), and a file becomes visible only after
its size has been charged, so a refusal leaves nothing behind.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from spark.database import files as files_db
from spark.files.store import MountedVolumeStore

__all__ = [
    "DEFAULT_MAX_UPLOAD_MB",
    "DEFAULT_SERVER_QUOTA_MB",
    "FileArea",
    "FileAreaNotConfigured",
    "FileLimits",
]

DEFAULT_MAX_UPLOAD_MB = 50
DEFAULT_SERVER_QUOTA_MB = 1024
_MB = 1024 * 1024


class SettingsLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


class BridgeLike(Protocol):
    def call(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any: ...


class FileAreaNotConfigured(RuntimeError):
    """No file storage is configured for this process."""


def _megabytes(settings: SettingsLike, key: str, default: float | None) -> int | None:
    """A size setting in MB as bytes; 0 means no limit; unusable values give the default."""
    value = settings.get(key, None)
    try:
        if isinstance(value, bool) or value is None:
            raise TypeError(key)
        number = float(value)
    except (TypeError, ValueError):
        number = -1.0
    if number < 0:
        if default is None:
            return None
        number = default
    return None if number == 0 else int(number * _MB)


@dataclass(frozen=True)
class FileLimits:
    """Administrator limits; None means no limit."""

    max_upload_bytes: int | None
    quota_bytes: int | None

    @classmethod
    def from_settings(cls, settings: SettingsLike, *, server: bool) -> FileLimits:
        """Read ``files.max_upload_mb`` and ``files.quota_mb`` (per user).

        Without a quota setting, server mode gives each user 1 GB and personal
        mode has no quota.
        """
        return cls(
            max_upload_bytes=_megabytes(settings, "files.max_upload_mb", DEFAULT_MAX_UPLOAD_MB),
            quota_bytes=_megabytes(
                settings, "files.quota_mb", DEFAULT_SERVER_QUOTA_MB if server else None
            ),
        )


class FileArea:
    """Per-user files: a store, the bridge for metadata, and live limits."""

    def __init__(
        self,
        store: MountedVolumeStore,
        db: BridgeLike,
        limits: Callable[[], FileLimits],
    ) -> None:
        self.store = store
        self._db = db
        self._limits = limits

    def limits(self) -> FileLimits:
        return self._limits()

    def _charge(self, user_guid: str, new: files_db.NewUserFile) -> int:
        file_id: int = self._db.call(
            files_db.record_user_file, user_guid, new, quota_bytes=self._limits().quota_bytes
        )
        return file_id

    def save(
        self,
        user_guid: str,
        key: str,
        data: bytes | BinaryIO,
        *,
        filename: str,
        mime_type: str | None = None,
        conversation_id: int | None = None,
        source: str = "upload",
    ) -> int:
        """Store an upload under the upload limit and the user's quota; return its id.

        Raises :class:`~spark.files.store.UploadTooLarge`,
        :class:`~spark.database.files.QuotaExceeded` or
        :class:`~spark.files.paths.PathRefused`.
        """
        recorded: list[int] = []

        def charge(size: int) -> None:
            recorded.append(
                self._charge(
                    user_guid,
                    files_db.NewUserFile(
                        key=key,
                        filename=filename,
                        size=size,
                        mime_type=mime_type,
                        conversation_id=conversation_id,
                        source=source,
                    ),
                )
            )

        self.store.put(
            user_guid,
            key,
            data,
            max_bytes=self._limits().max_upload_bytes,
            before_commit=charge,
        )
        return recorded[0]

    def staging_dir(self, user_guid: str) -> Path:
        """A private directory inside the user's area for work in progress."""
        staging = self.store.area(user_guid) / ".staging"
        staging.mkdir(exist_ok=True)
        return staging

    def create(
        self,
        user_guid: str,
        key: str,
        writer: Callable[[Path], None],
        *,
        conversation_id: int | None = None,
        mime_type: str | None = None,
    ) -> Path:
        """Have ``writer`` produce a file, charge it, then move it to ``key``.

        ``writer`` receives a staging path with the key's suffix. Returns the
        final local path. On any failure nothing is stored or recorded.
        """
        target = self.store.local_path(user_guid, key)
        return self.create_many(
            user_guid, [(target, writer)], conversation_id=conversation_id, mime_type=mime_type
        )[0]

    def create_many(
        self,
        user_guid: str,
        targets: Sequence[tuple[Path, Callable[[Path], None]]],
        *,
        conversation_id: int | None = None,
        mime_type: str | None = None,
        source: str = "created",
    ) -> list[Path]:
        """Produce several files, charge them in one transaction, then move them in.

        Each target must be a path inside the user's area (from
        :meth:`MountedVolumeStore.local_path`). All or nothing.
        """
        area = self.store.area(user_guid)
        staged: list[tuple[Path, Path]] = []
        try:
            for target, writer in targets:
                if not target.is_relative_to(area) or target == area:
                    raise ValueError("a target outside the user's area")
                fd, tmp_name = tempfile.mkstemp(
                    dir=self.staging_dir(user_guid), suffix=target.suffix
                )
                os.close(fd)
                staged.append((Path(tmp_name), target))
                writer(Path(tmp_name))
            self._db.call(
                files_db.record_user_files,
                user_guid,
                [
                    files_db.NewUserFile(
                        key=target.relative_to(area).as_posix(),
                        filename=target.name,
                        size=tmp.stat().st_size,
                        mime_type=mime_type,
                        conversation_id=conversation_id,
                        source=source,
                    )
                    for tmp, target in staged
                ],
                quota_bytes=self._limits().quota_bytes,
            )
            for tmp, target in staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, target)
        finally:
            for tmp, _ in staged:
                tmp.unlink(missing_ok=True)
        return [target for _, target in staged]

    def check_space(self, user_guid: str, nbytes: int) -> None:
        """Raise :class:`QuotaExceeded` if ``nbytes`` more would exceed the quota now.

        An early refusal only: the charge itself is still checked atomically.
        """
        quota = self._limits().quota_bytes
        if quota is None:
            return
        used: int = self._db.call(files_db.get_usage, user_guid)
        if used + nbytes > quota:
            raise files_db.QuotaExceeded(used, nbytes, quota)
