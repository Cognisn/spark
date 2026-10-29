"""Where the document, document creation and archive tools may read and write.

Server mode: the signed-in user's area only, addressed by area-relative keys;
files the tools write are charged to the user's quota and recorded. Personal
mode: the configured allowed roots (``embedded_tools.filesystem.allowed_paths``),
by absolute path or relative to the first root. Both go through the one
resolved-path check in :mod:`spark.files.paths`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from spark.files.area import FileArea
from spark.files.paths import contain, contain_any

__all__ = ["ToolScope", "ToolScopeUnavailable", "Writer", "tool_scope"]

Writer = Callable[[Path], None]


class ToolScopeUnavailable(RuntimeError):
    """The tool has nowhere it may read or write."""


class ToolScope:
    """The permitted area for one tool call."""

    def __init__(
        self,
        roots: Sequence[Path],
        *,
        area: FileArea | None = None,
        user_guid: str | None = None,
        conversation_id: int | None = None,
    ) -> None:
        self.roots = tuple(roots)
        self.area = area
        self.user_guid = user_guid
        self.conversation_id = conversation_id

    @classmethod
    def personal(cls, roots: Sequence[Path]) -> ToolScope:
        if not roots:
            raise ToolScopeUnavailable(
                "File tools need allowed_paths to be configured (Settings, Filesystem)."
            )
        return cls(roots)

    @classmethod
    def server(
        cls, area: FileArea, user_guid: str, *, conversation_id: int | None = None
    ) -> ToolScope:
        return cls(
            [area.store.area(user_guid)],
            area=area,
            user_guid=user_guid,
            conversation_id=conversation_id,
        )

    @property
    def is_server(self) -> bool:
        return self.area is not None

    @property
    def area_root(self) -> Path:
        return self.roots[0]

    def resolve(self, path: str) -> Path:
        """The contained path for ``path``; raises :class:`~spark.files.paths.PathRefused`."""
        if self.is_server:
            return contain(self.area_root, path)
        return contain_any(self.roots, path)

    def show(self, path: Path) -> str:
        """How to name ``path`` to the model: the key in server mode (never the volume path)."""
        if self.is_server:
            return path.relative_to(self.area_root.resolve()).as_posix() or "."
        return str(path)

    @contextmanager
    def staging(self) -> Iterator[Path]:
        """A private scratch directory on the same volume as the targets."""
        base = (
            self.area.staging_dir(self.user_guid)
            if self.area is not None and self.user_guid is not None
            else None
        )
        tmp = Path(tempfile.mkdtemp(dir=base))
        try:
            yield tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def check_space(self, nbytes: int) -> None:
        """Refuse early when ``nbytes`` more would exceed the user's quota (server mode)."""
        if self.area is not None and self.user_guid is not None:
            self.area.check_space(self.user_guid, nbytes)

    def write(self, targets: Sequence[tuple[Path, Writer]]) -> list[Path]:
        """Produce each contained target with its writer, all or nothing.

        Server mode stages every file, charges them in one transaction, then
        moves them into place. Personal mode writes in place.
        """
        if self.area is not None and self.user_guid is not None:
            return self.area.create_many(
                self.user_guid, targets, conversation_id=self.conversation_id
            )
        for target, writer in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            writer(target)
        return [t for t, _ in targets]


def _allowed_roots(config: dict[str, Any]) -> list[Path]:
    fs = (config.get("embedded_tools") or {}).get("filesystem") or {}
    raw = fs.get("allowed_paths") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [Path(os.path.expanduser(str(p).strip())) for p in raw if str(p).strip()]


def tool_scope(config: dict[str, Any], *, server: bool) -> ToolScope:
    """Build the scope for a tool call from the registry's runtime ``config``.

    Server mode needs the signed-in user (``_user_guid``, or the knowledge
    graph's ``_kg_user_guid``) and the bridge (``_files_db`` or ``_kg_db``).
    """
    if not server:
        return ToolScope.personal(_allowed_roots(config))
    user = config.get("_user_guid") or config.get("_kg_user_guid")
    db = config.get("_files_db") or config.get("_kg_db")
    if not user or db is None:
        raise ToolScopeUnavailable("File tools need a signed-in user's file area.")
    from spark import files as files_pkg

    cid = config.get("_kg_conversation_id")
    return ToolScope.server(
        files_pkg.file_area(db), str(user), conversation_id=int(cid) if cid else None
    )
