"""Per-user file areas: storage, containment, limits and quotas.

:func:`configure` is called once at start-up (by
``spark.core.application.configure_runtime``) with the settings; the store and
the limits are then built from the current settings on each call, so limits an
administrator edits apply at once.

Settings keys: ``files.root`` (the storage root; required in server mode,
defaulting to ``<data directory>/files`` in personal mode),
``files.max_upload_mb`` and ``files.quota_mb`` (per user; 0 means no limit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spark.files.area import FileArea, FileAreaNotConfigured, FileLimits, SettingsLike
from spark.files.paths import PathRefused, contain, contain_any
from spark.files.store import FileStore, MountedVolumeStore, StoredFile, UploadTooLarge
from spark.mode import Mode

__all__ = [
    "FileArea",
    "FileAreaNotConfigured",
    "FileLimits",
    "FileStore",
    "MountedVolumeStore",
    "PathRefused",
    "StoredFile",
    "UploadTooLarge",
    "configure",
    "contain",
    "contain_any",
    "file_area",
    "file_limits",
    "file_store",
    "reset",
]

_settings: SettingsLike | None = None
_data_dir: Path | None = None
_mode: Mode | None = None


def configure(settings: SettingsLike, *, data_dir: Path, runtime_mode: Mode) -> None:
    """Record the settings, data directory and mode the file area is built from."""
    global _settings, _data_dir, _mode
    _settings, _data_dir, _mode = settings, data_dir, runtime_mode


def reset() -> None:
    """Forget the configuration (tests only)."""
    global _settings, _data_dir, _mode
    _settings = _data_dir = _mode = None


def _configured() -> tuple[SettingsLike, Path, Mode]:
    if _settings is None or _data_dir is None or _mode is None:
        raise FileAreaNotConfigured("file storage has not been configured")
    return _settings, _data_dir, _mode


def file_store() -> MountedVolumeStore:
    """The store for the configured root."""
    settings, data_dir, mode = _configured()
    root = settings.get("files.root", None)
    if root:
        return MountedVolumeStore(Path(str(root)).expanduser())
    if mode is Mode.SERVER:
        raise FileAreaNotConfigured("server mode needs files.root (the file storage volume)")
    return MountedVolumeStore(data_dir / "files")


def file_limits() -> FileLimits:
    """The limits as currently configured."""
    settings, _, mode = _configured()
    return FileLimits.from_settings(settings, server=mode is Mode.SERVER)


def file_area(db: Any) -> FileArea:
    """A :class:`FileArea` on the configured store, using the bridge ``db``."""
    return FileArea(file_store(), db, file_limits)
