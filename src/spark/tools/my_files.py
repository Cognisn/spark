"""The signed-in user's own files: list_my_files and read_my_file.

Files a user attaches in chat are uploaded to their file area (A7); the model
hears of them by name and id in the message, never by content, and reads them
with these tools. Both act for the turn's user only (``_user_guid``), through
:mod:`spark.files` and the bridge the turn carries. Another user's file id is
answered exactly as an id that does not exist. Personal mode reads the
personal area (``<data dir>/files``), whatever allowed_paths says.
"""

from __future__ import annotations

from typing import Any, Final

from spark import files as files_pkg
from spark.database import files as files_db
from spark.db.engine import Row
from spark.files.area import BridgeLike, FileArea
from spark.files.scope import ToolScope, ToolScopeUnavailable

__all__ = ["TOOL_NAMES", "execute", "get_tools"]

TOOL_NAMES: Final = frozenset({"list_my_files", "read_my_file"})

DEFAULT_MAX_CHARS: Final = 20_000
MAX_CHARS: Final = 100_000
# Text files are read up to this many bytes; the rest is not shown.
MAX_TEXT_BYTES: Final = 4 * 1024 * 1024

# Suffix -> the document tool that reads it.
_DOCUMENT_READERS: Final = {
    ".docx": "read_word",
    ".xlsx": "read_excel",
    ".pdf": "read_pdf",
    ".pptx": "read_powerpoint",
}

_TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "list_my_files",
        "description": (
            "List the files in the user's own file area: files they attached in chat and "
            "documents created for them. Each entry gives the file id to pass to read_my_file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_only": {
                    "type": "boolean",
                    "description": "Only files attached to or created in this conversation. "
                    "Default: false.",
                },
            },
        },
    },
    {
        "name": "read_my_file",
        "description": (
            "Read one of the user's own files by its file id (from an attachment note or "
            "list_my_files). Word, Excel, PowerPoint and PDF files are read as text; other "
            "text files are returned in pages of characters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "The file id."},
                "offset": {
                    "type": "integer",
                    "description": "Character to start from, for text files. Default: 0.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"Characters to return, for text files. Default: "
                    f"{DEFAULT_MAX_CHARS}; at most {MAX_CHARS}.",
                },
            },
            "required": ["file_id"],
        },
    },
]


def get_tools() -> list[dict[str, Any]]:
    """The tool definitions."""
    return [dict(t) for t in _TOOLS]


def _whose(config: dict[str, Any]) -> tuple[str, BridgeLike]:
    """The turn's user and bridge; refused without either (never another user)."""
    user = config.get("_user_guid")
    db = config.get("_files_db") or config.get("_kg_db")
    if not user or db is None:
        raise ToolScopeUnavailable("File tools need a signed-in user's file area.")
    return str(user), db


def _int(value: Any, default: int, *, lowest: int, highest: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        number = int(value)
    except ValueError:
        return default
    number = max(lowest, number)
    return min(highest, number) if highest is not None else number


def _file_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _describe(row: Row) -> str:
    mime = row["mime_type"] or "unknown type"
    return f"{row['filename']} ({mime}, {int(row['size_bytes']):,} bytes)"


def _list(user: str, db: BridgeLike, args: dict[str, Any], config: dict[str, Any]) -> str:
    rows: list[Row] = db.call(files_db.list_user_files, user)
    cid = config.get("_kg_conversation_id")
    if args.get("conversation_only") is True:
        rows = [r for r in rows if cid is not None and r["conversation_id"] == int(cid)]
    if not rows:
        return (
            "You have no files."
            if not args.get("conversation_only")
            else ("No files are attached to this conversation.")
        )
    lines = ["Your files (read one with read_my_file and its file id):"]
    for row in rows:
        here = (
            " [this conversation]" if cid is not None and row["conversation_id"] == int(cid) else ""
        )
        lines.append(f"- file {row['id']}: {_describe(row)}, added {row['created_at']}{here}")
    return "\n".join(lines)


def _read_text(area: FileArea, user: str, row: Row, args: dict[str, Any]) -> tuple[str, bool]:
    path = area.store.local_path(user, str(row["storage_key"]))
    with path.open("rb") as handle:
        data = handle.read(MAX_TEXT_BYTES)
    if b"\x00" in data:
        return (
            f"File {row['id']} ({row['filename']}) is a binary file and cannot be read as text.",
            True,
        )
    text = data.decode("utf-8", errors="replace")
    offset = _int(args.get("offset"), 0, lowest=0)
    limit = _int(args.get("max_chars"), DEFAULT_MAX_CHARS, lowest=1, highest=MAX_CHARS)
    page = text[offset : offset + limit]
    end = offset + len(page)
    header = (
        f"File {row['id']}: {_describe(row)}. Characters {offset:,} to {end:,} of {len(text):,}"
    )
    if end < len(text):
        header += f"; read on with offset {end}"
    elif len(data) == MAX_TEXT_BYTES:
        header += f"; only the first {MAX_TEXT_BYTES:,} bytes can be read"
    return f"{header}.\n\n{page}", False


def _read(user: str, db: BridgeLike, args: dict[str, Any]) -> tuple[str, bool]:
    file_id = _file_id(args.get("file_id"))
    if file_id is None:
        return "read_my_file needs file_id: a file id from list_my_files.", True
    not_found = f"File {file_id} was not found in your files.", True
    row: Row | None = db.call(files_db.get_user_file, user, file_id)
    if row is None:
        return not_found
    area = files_pkg.file_area(db)
    key = str(row["storage_key"])
    if not area.store.local_path(user, key).is_file():
        return not_found
    reader = _DOCUMENT_READERS.get(f".{key.rsplit('.', 1)[-1].lower()}" if "." in key else "")
    if reader is not None:
        from spark.tools.documents import execute as read_document

        content = read_document(reader, {"path": key}, scope=ToolScope.server(area, user))
        return f"File {row['id']}: {_describe(row)}.\n\n{content}", False
    return _read_text(area, user, row, args)


def execute(tool_name: str, tool_input: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    """Run ``tool_name`` for the turn's user; returns (text, is_error).

    Raises :class:`~spark.files.scope.ToolScopeUnavailable` without a user or
    bridge, and :class:`~spark.files.area.FileAreaNotConfigured` without storage.
    """
    user, db = _whose(config)
    if tool_name == "list_my_files":
        return _list(user, db, tool_input, config), False
    if tool_name == "read_my_file":
        return _read(user, db, tool_input)
    return f"Unknown file tool: {tool_name}", True
