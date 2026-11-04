"""Chat attachments: name the user's own files in the message the model receives.

The chat composer uploads each attached file to the user's area
(``/files/api``) and sends the message with the file ids. The send routes call
:func:`attach`: every id must be one of the signed-in user's own files (another
user's id, or one that does not exist, answers 404 and the turn never runs).
The message then ends with a note naming each file by id and name, never its
content; the model reads a file with the read_my_file tool. The chat page
parses the same note back into chips (``static/js/attachments.js``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import Request
from fastapi.responses import JSONResponse

from spark.database import files as files_db
from spark.web.db import database
from spark.web.scope import not_found

__all__ = [
    "MARKER",
    "MAX_ATTACHMENTS",
    "AttachmentError",
    "attach",
    "parse_ids",
    "with_attachments",
]

MARKER: Final = "[Attached files: read them with the read_my_file tool]"
MAX_ATTACHMENTS: Final = 20

_LINE_BREAKS = re.compile(r"[\x00-\x1f\x7f  ]+")


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


def _name(filename: object) -> str:
    return _LINE_BREAKS.sub(" ", str(filename)).strip() or "file"


def with_attachments(message: str, files: Sequence[Mapping[str, Any]]) -> str:
    """``message`` followed by the note naming ``files`` (each with ``id`` and ``filename``)."""
    if not files:
        return message
    lines = [MARKER, *(f"- file {int(f['id'])}: {_name(f['filename'])}" for f in files)]
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
