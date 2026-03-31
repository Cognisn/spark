"""Archive tools — list and extract ZIP and TAR files."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Any

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


def execute(tool_name: str, tool_input: dict[str, Any], mode: str = "list") -> str:
    """Execute an archive tool."""
    path = Path(tool_input["path"]).resolve()
    if not path.is_file():
        return f"File not found: {path}"

    if tool_name == "list_archive":
        return _list_archive(path)
    elif tool_name == "extract_archive":
        if mode != "extract":
            return "Archive extraction is not enabled."
        dest = Path(tool_input["destination"]).resolve()
        return _extract_archive(path, dest)

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

    elif suffix in (".tar", ".gz", ".tgz", ".bz2"):
        with tarfile.open(str(path)) as tf:
            entries = []
            for member in tf.getmembers():
                kind = "d" if member.isdir() else "f"
                entries.append(f"  [{kind}] {member.name} ({member.size:,} bytes)")
            return f"Archive: {path.name} ({len(entries)} entries)\n" + "\n".join(entries[:200])

    return f"Unsupported archive format: {suffix}"


def _extract_archive(path: Path, destination: Path) -> str:
    destination.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()

    if suffix == ".zip":
        with zipfile.ZipFile(str(path)) as zf:
            zf.extractall(str(destination))
            return f"Extracted {len(zf.namelist())} files to {destination}"

    elif suffix in (".tar", ".gz", ".tgz", ".bz2"):
        with tarfile.open(str(path)) as tf:
            tf.extractall(str(destination), filter="data")
            return f"Extracted {len(tf.getmembers())} entries to {destination}"

    return f"Unsupported archive format: {suffix}"
