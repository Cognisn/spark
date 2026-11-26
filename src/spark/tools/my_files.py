"""The signed-in user's own files: list_my_files and read_my_file.

Files a user attaches in chat are uploaded to their file area (A7); the model
hears of them by name and id in the message, never by content, and reads them
with these tools. Both act for the turn's user only (``_user_guid``), through
:mod:`spark.files` and the bridge the turn carries. Another user's file id is
answered exactly as an id that does not exist. Personal mode reads the
personal area (``<data dir>/files``), whatever allowed_paths says.

In a conversation in a project (PC7, PC11) both tools also reach the resources the
conversation inherits from every level of its project path: listed by level, and read
by id from the project owner's area, for whoever sends the turn, as long as they may read
the conversation (:func:`spark.database.projects.resource_for_conversation`). Another
project's resource answers as a file that does not exist.

The attachment note lives here too, beside the tool it names: the send routes
end a message with it (:mod:`spark.web.attachments`), :func:`split_note` reads
it back (the current marker and every earlier one), and a turn that carries
attachments offers these tools first with one line naming the files
(:func:`attached_files`, :func:`attachment_context`, :func:`attachment_tools_first`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from spark import files as files_pkg
from spark.database import files as files_db
from spark.db.engine import Row
from spark.files.area import BridgeLike, FileArea
from spark.files.scope import ToolScope, ToolScopeUnavailable

__all__ = [
    "ATTACHMENT_TOOLS",
    "EARLIER_NOTE_MARKERS",
    "NOTE_MARKER",
    "NOTE_MARKERS",
    "TOOL_NAMES",
    "attached_files",
    "attachment_context",
    "attachment_tools_first",
    "execute",
    "get_tools",
    "note_name",
    "split_note",
]

TOOL_NAMES: Final = frozenset({"list_my_files", "read_my_file"})

# The tools a turn with attachments offers first, in this order.
ATTACHMENT_TOOLS: Final = ("read_my_file", "list_my_files")

# The line that opens the attachment note, and the lines it opened before (still
# stored in conversations, so still read back).
NOTE_MARKER: Final = "[Attached files: call read_my_file with the file id to read each one]"
EARLIER_NOTE_MARKERS: Final = ("[Attached files: read them with the read_my_file tool]",)
NOTE_MARKERS: Final = (NOTE_MARKER, *EARLIER_NOTE_MARKERS)
_NOTE_LINE: Final = re.compile(r"^- file (\d+): (.+)$")
_LINE_BREAKS: Final = re.compile(r"[\x00-\x1f\x7f  ]+")

# At most this many files are named in a turn's attachment line.
MAX_NAMED: Final = 20

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


def _project_levels(user: str, db: BridgeLike, config: dict[str, Any]) -> list[Any]:
    """The turn's conversation's project path with its resources (none outside a project,
    or when the user may not read the conversation)."""
    cid = config.get("_kg_conversation_id")
    if cid is None:
        return []
    from spark.database import projects as projects_db
    from spark.database.scope import NotOwned

    try:
        levels: list[Any] = db.call(projects_db.conversation_resources, user, int(cid))
    except NotOwned:
        return []
    return levels


def _project_lines(levels: Sequence[Any]) -> list[str]:
    if not any(level.resources for level in levels):
        return []
    from spark.projects.prompt import neutral_name

    lines = ["Project resources of this conversation (read one with read_my_file and its id):"]
    for level in levels:
        for row in level.resources:
            lines.append(
                f"- file {row['file_id']}: {_describe(row)}, from level {level.level} "
                f'("{neutral_name(level.name)}")'
            )
    return lines


def _list(user: str, db: BridgeLike, args: dict[str, Any], config: dict[str, Any]) -> str:
    rows: list[Row] = db.call(files_db.list_user_files, user)
    cid = config.get("_kg_conversation_id")
    if args.get("conversation_only") is True:
        rows = [r for r in rows if cid is not None and r["conversation_id"] == int(cid)]
    project = _project_lines(_project_levels(user, db, config))
    if not rows and not project:
        return (
            "You have no files."
            if not args.get("conversation_only")
            else ("No files are attached to this conversation.")
        )
    lines = ["Your files (read one with read_my_file and its file id):"] if rows else []
    for row in rows:
        here = (
            " [this conversation]" if cid is not None and row["conversation_id"] == int(cid) else ""
        )
        lines.append(
            f"- file {row['id']}: {_describe(row)}, added {row['created_at']}, "
            f"key {row['storage_key']}{here}"
        )
    if project:
        if lines:
            lines.append("")
        lines.extend(project)
    return "\n".join(lines)


def _inherited(user: str, db: BridgeLike, config: dict[str, Any], file_id: int) -> Row | None:
    """A resource the turn's conversation inherits from its project path, as a file row of
    the project owner's area (``user_guid`` names the owner), or None."""
    cid = config.get("_kg_conversation_id")
    if cid is None:
        return None
    from spark.database import projects as projects_db

    found: Row | None = db.call(projects_db.resource_for_conversation, user, int(cid), file_id)
    if found is None:
        return None
    return {**found, "id": int(found["file_id"]), "user_guid": found["owner_guid"]}


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


def _read(
    user: str, db: BridgeLike, args: dict[str, Any], config: dict[str, Any]
) -> tuple[str, bool]:
    file_id = _file_id(args.get("file_id"))
    if file_id is None:
        return "read_my_file needs file_id: a file id from list_my_files.", True
    not_found = f"File {file_id} was not found in your files.", True
    row: Row | None = db.call(files_db.get_user_file, user, file_id)
    # A project resource the conversation inherits is read from its owner's area.
    owner = user
    if row is None:
        row = _inherited(user, db, config, file_id)
        if row is None:
            return not_found
        owner = str(row["user_guid"])
    area = files_pkg.file_area(db)
    key = str(row["storage_key"])
    if not area.store.local_path(owner, key).is_file():
        return not_found
    reader = _DOCUMENT_READERS.get(f".{key.rsplit('.', 1)[-1].lower()}" if "." in key else "")
    if reader is not None:
        from spark.tools.documents import execute as read_document

        content = read_document(reader, {"path": key}, scope=ToolScope.server(area, owner))
        return f"File {row['id']}: {_describe(row)}.\n\n{content}", False
    return _read_text(area, owner, row, args)


# -- The attachment note and the turn's attachments ---------------------------------------


def note_name(filename: object) -> str:
    """A file name as one line of the note: control and line separator characters replaced."""
    return _LINE_BREAKS.sub(" ", str(filename)).strip() or "file"


def split_note(content: str) -> tuple[str, list[tuple[int, str]]]:
    """The message text and the (file id, name) pairs of the note that ends it.

    The note counts only when it ends the message and every line after its
    marker names a file; the marker may be the current one or an earlier one.
    Anything else is plain text with no files (as ``attachments.js`` parse()).
    """
    text = str(content or "")
    for marker in NOTE_MARKERS:
        if text.startswith(marker + "\n"):
            start, body = 0, text[len(marker) + 1 :]
        else:
            at = text.rfind("\n\n" + marker + "\n")
            if at < 0:
                continue
            start, body = at, text[at + 2 + len(marker) + 1 :]
        files: list[tuple[int, str]] = []
        for line in body.split("\n"):
            match = _NOTE_LINE.match(line)
            if match is None:
                files = []
                break
            files.append((int(match.group(1)), match.group(2)))
        if files:
            return text[:start], files
    return text, []


def attached_files(
    db: BridgeLike, user_guid: str, conversation_id: int | None, message: str
) -> list[Row]:
    """The user's own files this turn carries: named by the message's note, or uploaded
    to this conversation. Ids that are not the user's own are dropped."""
    named = {fid for fid, _ in split_note(message)[1]}
    rows: list[Row] = db.call(files_db.list_user_files, user_guid)
    return [
        r
        for r in rows
        if r["id"] in named
        or (
            conversation_id is not None
            and r["conversation_id"] == conversation_id
            and r["source"] == "upload"
        )
    ]


def attachment_context(rows: Sequence[Row]) -> str:
    """One line naming the turn's attached files by id, and the call that reads them."""
    if not rows:
        return ""
    named = ", ".join(f"file {r['id']} ({note_name(r['filename'])})" for r in rows[:MAX_NAMED])
    more = f" and {len(rows) - MAX_NAMED} more" if len(rows) > MAX_NAMED else ""
    return (
        f"Attached files: {named}{more}. Read each with read_my_file and its file id "
        "(list_my_files lists them all)."
    )


def attachment_tools_first(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """``tools`` with read_my_file and list_my_files first; every other tool kept, in order."""
    first = [t for name in ATTACHMENT_TOOLS for t in tools if t.get("name") == name]
    return first + [t for t in tools if t.get("name") not in ATTACHMENT_TOOLS]


def execute(tool_name: str, tool_input: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    """Run ``tool_name`` for the turn's user; returns (text, is_error).

    Raises :class:`~spark.files.scope.ToolScopeUnavailable` without a user or
    bridge, and :class:`~spark.files.area.FileAreaNotConfigured` without storage.
    """
    user, db = _whose(config)
    if tool_name == "list_my_files":
        return _list(user, db, tool_input, config), False
    if tool_name == "read_my_file":
        return _read(user, db, tool_input, config)
    return f"Unknown file tool: {tool_name}", True
