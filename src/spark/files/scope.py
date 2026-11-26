"""Where the document, document creation and archive tools may read and write.

Server mode: the signed-in user's area only, addressed by area-relative keys;
files the tools write are charged to the user's quota and recorded. Personal
mode: the configured allowed roots (``embedded_tools.filesystem.allowed_paths``),
by absolute path or relative to the first root. Both go through the one
resolved-path check in :mod:`spark.files.paths`.

A model often names a chat attachment by its bare file name, while uploads are
stored under ``uploads/<id>/<name>``. In server mode :meth:`ToolScope.locate`
looks such a name up among the scope's user's own recorded files (through the
database layer for that user, never a walk of the volume), and uses it only
when exactly one file matches.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from spark.database import files as files_db
from spark.db.engine import Row
from spark.files.area import BridgeLike, FileArea
from spark.files.paths import contain, contain_any

__all__ = ["Keeper", "Located", "ToolScope", "ToolScopeUnavailable", "Writer", "tool_scope"]

# How many of the user's most recent files a name that matches nothing lists.
RECENT_FILES: Final = 5

GUIDANCE: Final = (
    "Files attached in chat are read with read_my_file and their file id; "
    "list_my_files lists all your files."
)

Writer = Callable[[Path], None]


class ToolScopeUnavailable(RuntimeError):
    """The tool has nowhere it may read or write."""


@dataclass(frozen=True)
class Located:
    """What :meth:`ToolScope.locate` found: a file to use, or the answer to give instead.

    ``header`` names a file found by its name (one line, area key only); it is
    empty when the path itself resolved.
    """

    path: Path | None
    header: str = ""
    answer: str = ""

    def report(self, result: str) -> str:
        """The tool's ``result``, after the header when there is one."""
        return f"{self.header}\n\n{result}" if self.header else result


def _bare_name(text: str) -> str | None:
    """``text`` when it is a file name with no directory part, else None."""
    name = PurePosixPath(text.replace("\\", "/")).name
    return name if name and name == text else None


def _file_line(row: Row) -> str:
    return f"- file {row['id']}: {row['filename']}"


@dataclass(frozen=True)
class Keeper:
    """Personal mode's file area, where a document a create tool writes to an allowed
    root is also kept, so it has a file id and appears with the conversation's artefacts."""

    area: FileArea
    user_guid: str
    conversation_id: int | None = None


class ToolScope:
    """The permitted area for one tool call."""

    def __init__(
        self,
        roots: Sequence[Path],
        *,
        area: FileArea | None = None,
        user_guid: str | None = None,
        conversation_id: int | None = None,
        db: BridgeLike | None = None,
        keeper: Keeper | None = None,
    ) -> None:
        self.roots = tuple(roots)
        self.area = area
        self.user_guid = user_guid
        self.conversation_id = conversation_id
        self.db = db
        self.keeper = keeper

    @classmethod
    def personal(cls, roots: Sequence[Path], *, keeper: Keeper | None = None) -> ToolScope:
        if not roots:
            raise ToolScopeUnavailable(
                "File tools need allowed_paths to be configured (Settings, Filesystem)."
            )
        return cls(roots, keeper=keeper)

    @classmethod
    def server(
        cls,
        area: FileArea,
        user_guid: str,
        *,
        conversation_id: int | None = None,
        db: BridgeLike | None = None,
    ) -> ToolScope:
        """The user's area. With ``db`` (the turn's bridge), bare names are looked up."""
        return cls(
            [area.store.area(user_guid)],
            area=area,
            user_guid=user_guid,
            conversation_id=conversation_id,
            db=db,
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

    def locate(self, text: str) -> Located:
        """The existing file ``text`` names, or the answer to give when there is none.

        Raises :class:`~spark.files.paths.PathRefused` for a path outside the
        scope, before any lookup. A path that resolves to a file is used as it
        is. In server mode a bare file name that does not resolve is looked up
        among the scope's user's own recorded files: exact names before
        case-insensitive ones, this conversation's files before others. One
        match is used; several, or none, are answered with file ids.
        """
        path = self.resolve(text)
        if path.is_file():
            return Located(path)
        not_found = f"File not found: {self.show(path)}"
        if not self.is_server:
            return Located(None, answer=not_found)
        name = _bare_name(text)
        if name is None or self.db is None or self.user_guid is None:
            return Located(None, answer=f"{not_found}\n{GUIDANCE}")
        return self._by_name(name)

    def _by_name(self, name: str) -> Located:
        """Look ``name`` up among this scope's user's own files, and no one else's."""
        assert self.db is not None and self.user_guid is not None
        rows: list[Row] = self.db.call(files_db.list_user_files, self.user_guid)
        folded = name.casefold()
        exact = [r for r in rows if r["filename"] == name]
        loose = [r for r in rows if str(r["filename"]).casefold() == folded]
        for matches in (exact, loose):
            if not matches:
                continue
            chosen = [r for r in matches if self._in_conversation(r)] or matches
            if len(chosen) > 1:
                return Located(None, answer=self._several(name, chosen))
            return self._use(chosen[0], rows)
        return Located(None, answer=self._none(name, rows))

    def _in_conversation(self, row: Row) -> bool:
        return self.conversation_id is not None and row["conversation_id"] == self.conversation_id

    def _use(self, row: Row, rows: list[Row]) -> Located:
        key = str(row["storage_key"])
        path = self.resolve(key)
        if not path.is_file():
            return Located(None, answer=self._none(str(row["filename"]), rows))
        return Located(path, header=f"Read file {row['id']}: {row['filename']} ({key})")

    @staticmethod
    def _several(name: str, rows: list[Row]) -> str:
        lines = [
            f"Several of your files are named {name}, so none was read. "
            "Call read_my_file with the file id of the one you mean:"
        ]
        lines.extend(
            f"{_file_line(r)}, {int(r['size_bytes']):,} bytes, uploaded {r['created_at']}"
            for r in rows
        )
        return "\n".join(lines)

    @staticmethod
    def _none(name: str, rows: list[Row]) -> str:
        lines = [f"{name} is not in your file area."]
        if rows:
            lines.append("Your most recent files:")
            lines.extend(_file_line(r) for r in rows[:RECENT_FILES])
        else:
            lines.append("You have no files.")
        lines.append(GUIDANCE)
        return "\n".join(lines)

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

    def write_artefact(
        self,
        target: Path,
        writer: Writer,
        *,
        title: str,
        kind: str,
        mime_type: str | None = None,
    ) -> tuple[Path, int | None]:
        """Produce one contained artefact with its title and kind; return it with its id.

        Server mode records it in the user's area (under the quota) as a file of the
        conversation. Personal mode writes it in place and, with a :class:`Keeper`,
        keeps a copy in the personal file area for its id; without one the id is None.
        """
        if self.area is not None and self.user_guid is not None:
            [(path, file_id)] = self.area.create_recorded(
                self.user_guid,
                [(target, writer)],
                conversation_id=self.conversation_id,
                mime_type=mime_type,
                artefacts=[(title, kind)],
            )
            return path, file_id
        target.parent.mkdir(parents=True, exist_ok=True)
        writer(target)
        keeper = self.keeper
        if keeper is None:
            return target, None
        with target.open("rb") as handle:
            kept = keeper.area.save(
                keeper.user_guid,
                f"created/{uuid.uuid4().hex}/{target.name}",
                handle,
                filename=target.name,
                mime_type=mime_type,
                conversation_id=keeper.conversation_id,
                source="created",
                title=title,
                kind=kind,
            )
        return target, kept


def _keeper(config: dict[str, Any]) -> Keeper | None:
    """Personal mode's file area for the signed-in local user, when there is one."""
    user = config.get("_user_guid") or config.get("_kg_user_guid")
    db = config.get("_files_db") or config.get("_kg_db")
    if not user or db is None:
        return None
    from spark import files as files_pkg
    from spark.files.area import FileAreaNotConfigured

    try:
        area = files_pkg.file_area(db)
    except FileAreaNotConfigured:
        return None
    cid = config.get("_kg_conversation_id")
    return Keeper(area, str(user), int(cid) if cid else None)


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
        return ToolScope.personal(_allowed_roots(config), keeper=_keeper(config))
    user = config.get("_user_guid") or config.get("_kg_user_guid")
    db = config.get("_files_db") or config.get("_kg_db")
    if not user or db is None:
        raise ToolScopeUnavailable("File tools need a signed-in user's file area.")
    from spark import files as files_pkg

    cid = config.get("_kg_conversation_id")
    return ToolScope.server(
        files_pkg.file_area(db), str(user), conversation_id=int(cid) if cid else None, db=db
    )
