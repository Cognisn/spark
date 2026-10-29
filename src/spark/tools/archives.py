"""Archive tools — list and extract ZIP and TAR files."""

from __future__ import annotations

import shutil
import stat
import tarfile
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import IO, Any

from spark.files.paths import PathRefused, contain
from spark.files.scope import ToolScope, Writer

_TOOLS = [
    {
        "name": "list_archive",
        "description": "List the contents of a ZIP or TAR archive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the archive file."},
            },
            "required": ["path"],
        },
    },
]

_EXTRACT_TOOLS = [
    {
        "name": "extract_archive",
        "description": "Extract files from a ZIP or TAR archive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the archive."},
                "destination": {"type": "string", "description": "Directory to extract to."},
            },
            "required": ["path", "destination"],
        },
    },
]


def get_tools(mode: str = "list") -> list[dict[str, Any]]:
    """Return archive tool definitions based on mode."""
    tools = list(_TOOLS)
    if mode == "extract":
        tools.extend(_EXTRACT_TOOLS)
    return tools


def execute(
    tool_name: str, tool_input: dict[str, Any], mode: str = "list", *, scope: ToolScope
) -> str:
    """Execute an archive tool on paths inside ``scope``.

    Raises :class:`~spark.files.paths.PathRefused` for a path outside it, or for
    an archive with a member that would land outside the destination or is a
    link or special file; nothing is extracted then.
    """
    path = scope.resolve(str(tool_input["path"]))
    if tool_name == "extract_archive" and mode != "extract":
        return "Archive extraction is not enabled."
    if not path.is_file():
        return f"File not found: {scope.show(path)}"

    if tool_name == "list_archive":
        return _list_archive(path)
    elif tool_name == "extract_archive":
        dest = scope.resolve(str(tool_input["destination"]))
        return _extract_archive(path, dest, scope)

    return f"Unknown archive tool: {tool_name}"


def _list_archive(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".zip":
        with zipfile.ZipFile(str(path)) as zf:
            entries = []
            for info in zf.infolist():
                size = info.file_size
                entries.append(f"  {info.filename} ({size:,} bytes)")
            return f"Archive: {path.name} ({len(entries)} files)\n" + "\n".join(entries[:200])

    elif suffix in _TAR_SUFFIXES:
        with tarfile.open(str(path)) as tf:
            entries = []
            for member in tf.getmembers():
                kind = "d" if member.isdir() else "f"
                entries.append(f"  [{kind}] {member.name} ({member.size:,} bytes)")
            return f"Archive: {path.name} ({len(entries)} entries)\n" + "\n".join(entries[:200])

    return f"Unsupported archive format: {suffix}"


_TAR_SUFFIXES = (".tar", ".gz", ".tgz", ".bz2")
_CHUNK = 1024 * 1024


def _zip_is_link(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _copier(open_member: Callable[[], IO[bytes]]) -> Writer:
    def write(target: Path) -> None:
        with open_member() as src, target.open("wb") as out:
            shutil.copyfileobj(src, out, _CHUNK)

    return write


def _extract_archive(path: Path, destination: Path, scope: ToolScope) -> str:
    """Check every member, then extract all of them or none."""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(str(path)) as zf:
            planned: list[tuple[Path, Writer]] = []
            total = 0
            for info in zf.infolist():
                if _zip_is_link(info):
                    raise PathRefused(f"the archive member {info.filename!r} is a link")
                target = contain(destination, info.filename)
                if info.is_dir():
                    continue
                total += info.file_size
                planned.append((target, _copier(partial(zf.open, info))))
            return _write_planned(planned, total, destination, scope)

    elif suffix in _TAR_SUFFIXES:
        with tarfile.open(str(path)) as tf:
            planned = []
            total = 0
            for member in tf.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise PathRefused(
                        f"the archive member {member.name!r} is a link or special file"
                    )
                target = contain(destination, member.name)
                if member.isdir():
                    continue
                total += member.size
                planned.append((target, _copier(partial(_tar_member, tf, member))))
            return _write_planned(planned, total, destination, scope)

    return f"Unsupported archive format: {suffix}"


def _tar_member(tf: tarfile.TarFile, member: tarfile.TarInfo) -> IO[bytes]:
    handle = tf.extractfile(member)
    if handle is None:
        raise PathRefused(f"the archive member {member.name!r} cannot be read")
    return handle


def _write_planned(
    planned: list[tuple[Path, Writer]], total: int, destination: Path, scope: ToolScope
) -> str:
    scope.check_space(total)
    scope.write(planned)
    return f"Extracted {len(planned)} files to {scope.show(destination)}"
