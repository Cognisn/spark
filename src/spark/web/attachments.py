"""Chat attachments: name the user's own files in the message the model receives.

The chat composer uploads each attached file to the user's area
(``/files/api``) and sends the message with the file ids. The send routes call
:func:`attach`: every id must be one of the signed-in user's own files (another
user's id, or one that does not exist, answers 404 and the turn never runs).
The message then ends with a note naming each file by id and name, never its
content, with the call that reads them (read_my_file and the file id). The chat
page parses the same note back into chips (``static/js/attachments.js``) and
:func:`split_note` reads it on the server; both accept every marker the note
has carried (:data:`spark.tools.my_files.NOTE_MARKERS`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from fastapi.responses import JSONResponse

from spark.database import files as files_db
from spark.tools.my_files import EARLIER_NOTE_MARKERS, NOTE_MARKER, note_name, split_note
from spark.web.db import database
from spark.web.scope import not_found

__all__ = [
    "EARLIER_MARKERS",
    "MARKER",
    "MAX_ATTACHMENTS",
    "AttachmentError",
    "attach",
    "parse_ids",
    "split_note",
    "with_attachments",
]

MARKER: Final = NOTE_MARKER
EARLIER_MARKERS: Final = EARLIER_NOTE_MARKERS
MAX_ATTACHMENTS: Final = 20


class AttachmentError(ValueError):
    """The attachment ids sent with a message cannot be used."""


def _one_id(value: Any) -> int:
    if isinstance(value, bool):
        raise AttachmentError("Attachment ids must be file ids.")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise AttachmentError("Attachment ids must be file ids.")


def parse_ids(raw: object) -> list[int]:
    """File ids from a query value (``"3,5"``) or a JSON list, duplicates dropped, in order."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        items: Sequence[Any] = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        raise AttachmentError("Attachment ids must be a list of file ids.")
    ids = list(dict.fromkeys(_one_id(item) for item in items))
    if len(ids) > MAX_ATTACHMENTS:
        raise AttachmentError(f"A message can carry at most {MAX_ATTACHMENTS} attachments.")
    return ids


def with_attachments(message: str, files: Sequence[Mapping[str, Any]]) -> str:
    """``message`` followed by the note naming ``files`` (each with ``id`` and ``filename``)."""
    if not files:
        return message
    lines = [MARKER, *(f"- file {int(f['id'])}: {note_name(f['filename'])}" for f in files)]
    note = "\n".join(lines)
    return f"{message}\n\n{note}" if message else note


async def attach(request: Request, user_guid: str, raw: object, message: str) -> str | JSONResponse:
    """The message with its attachments named, or the refusal to send (400 or 404)."""
    try:
        ids = parse_ids(raw)
    except AttachmentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    db = database(request)
    files = []
    for file_id in ids:
        row = await files_db.get_user_file(db, user_guid, file_id)
        if row is None:
            return not_found("Attached file not found")
        files.append(row)
    return with_attachments(message, files)
