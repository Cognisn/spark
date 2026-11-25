"""Showing a document: from a link or a file, to a file kept with the conversation.

:func:`show` is what the ``show_document`` tool runs, on the turn's worker thread, for
the turn's sender in the turn's conversation:

* **A link** is fetched under the fetch guard (:mod:`spark.preview.guard`), from the
  hosts of the MCP servers the sender is connected to (:mod:`spark.preview.sources`),
  and stored in the sender's file area as a file of the conversation (``source``
  ``preview``), under the attachment limit and the sender's quota.
* **A file** is one already in the conversation (uploaded into it, created in it, or
  shown before), or one of the sender's own files, which is copied into the
  conversation. Another person's file from elsewhere is refused: showing it would
  give this conversation's members a file they were never given.

Either way the result is a :class:`Card`: the file every member of the conversation
can open through the conversation's access decision (``GET
/preview/api/<conversation>/files/<file>``), with its name, type and size. The tool's
result carries the card on a line of its own (:data:`CARD_MARKER`), which the chat
page turns into a document card that reopens the preview.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Final

from spark import files as files_pkg
from spark.database import files as files_db
from spark.db.engine import Row
from spark.files.area import BridgeLike
from spark.files.store import UploadTooLarge
from spark.preview import guard, sources
from spark.preview.guard import LinkRefused, McpOrigin
from spark.preview.kinds import Kind, classify, content_matches, describe_type

__all__ = [
    "CARD_MARKER",
    "PREVIEW_SOURCE",
    "Card",
    "ShowRefused",
    "card_line",
    "parse_card",
    "result_text",
    "safe_name",
    "show",
]

# Begins the line of a tool result that carries the document card (JSON after it).
CARD_MARKER: Final = "[spark-preview]"
# user_files.source of a document fetched for the preview.
PREVIEW_SOURCE: Final = "preview"
_HEAD: Final = 8192
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._ ()+-]")
_TITLE: Final = 120


class ShowRefused(Exception):
    """The document cannot be shown; the message says why, plainly."""


@dataclass(frozen=True)
class Card:
    """A document shown in a conversation: the file members open, and how it is drawn."""

    file_id: int
    conversation_id: int
    name: str
    title: str
    type: str
    kind: str
    size: int


def safe_name(name: str | None) -> str:
    """A plain file name: no directories, streams or odd characters."""
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = _UNSAFE.sub("_", base).strip(" .")[:120].strip(" .")
    return cleaned or "document"


def _title(value: Any, fallback: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return (text or fallback)[:_TITLE]


def card_line(card: Card) -> str:
    """The tool result line that carries ``card``."""
    return f"{CARD_MARKER} {json.dumps(asdict(card), separators=(',', ':'))}"


def parse_card(text: str) -> Card | None:
    """The card a tool result carries, or None."""
    for line in str(text or "").splitlines():
        if not line.startswith(CARD_MARKER):
            continue
        try:
            data = json.loads(line[len(CARD_MARKER) :].strip())
            return Card(
                file_id=int(data["file_id"]),
                conversation_id=int(data["conversation_id"]),
                name=str(data["name"]),
                title=str(data["title"]),
                type=str(data["type"]),
                kind=str(data["kind"]),
                size=int(data["size"]),
            )
        except (ValueError, KeyError, TypeError):
            return None
    return None


def _card(row: Row, conversation_id: int, title: Any) -> Card:
    name = str(row["filename"])
    kind, media = classify(row["mime_type"], name)
    return Card(
        file_id=int(row["id"]),
        conversation_id=conversation_id,
        name=name,
        title=_title(title, name),
        type=media,
        kind=kind.value,
        size=int(row["size_bytes"]),
    )


def _checked(card: Card, head: bytes) -> Card:
    """The card, drawn as a plain file when its content is not what its type says."""
    if content_matches(Kind(card.kind), card.type, head):
        return card
    return Card(**{**asdict(card), "kind": Kind.OTHER.value})


def _file_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _head(owner: str, key: str) -> bytes:
    with files_pkg.file_store().open(owner, key) as handle:
        return handle.read(_HEAD)


def _from_file(db: BridgeLike, user: str, conversation_id: int, file_id: int, title: Any) -> Card:
    missing = ShowRefused(f"File {file_id} is not one of this conversation's files or yours.")
    row: Row | None = db.call(files_db.get_conversation_file, user, conversation_id, file_id)
    if row is None:
        own: Row | None = db.call(files_db.get_user_file, user, file_id)
        if own is None:
            raise missing
        row = _copy_in(db, user, conversation_id, own)
    try:
        head = _head(str(row["user_guid"]), str(row["storage_key"]))
    except (FileNotFoundError, OSError) as exc:
        raise missing from exc
    return _checked(_card(row, conversation_id, title), head)


def _copy_in(db: BridgeLike, user: str, conversation_id: int, own: Row) -> Row:
    """One of the sender's own files, copied into the conversation for its members."""
    area = files_pkg.file_area(db)
    name = safe_name(str(own["filename"]))
    try:
        with area.store.open(user, str(own["storage_key"])) as handle:
            new_id = area.save(
                user,
                f"previews/{uuid.uuid4().hex}/{name}",
                handle,
                filename=name,
                mime_type=own["mime_type"],
                conversation_id=conversation_id,
                source=PREVIEW_SOURCE,
            )
    except FileNotFoundError as exc:
        raise ShowRefused(f"File {own['id']} is no longer stored.") from exc
    except (UploadTooLarge, files_db.QuotaExceeded) as exc:
        raise ShowRefused(str(exc)) from exc
    row: Row | None = db.call(files_db.get_user_file, user, new_id)
    assert row is not None
    return row


def _from_link(
    db: BridgeLike,
    user: str,
    conversation_id: int,
    url: str,
    title: Any,
    origins: list[McpOrigin],
    fetcher: Callable[..., guard.Fetched],
    timeout: float,
) -> Card:
    area = files_pkg.file_area(db)
    max_bytes = area.limits().max_upload_bytes
    try:
        fetched = fetcher(url, origins, max_bytes=max_bytes, timeout=timeout)
    except LinkRefused as exc:
        raise ShowRefused(str(exc)) from exc
    name = safe_name(fetched.filename)
    try:
        new_id = area.save(
            user,
            f"previews/{uuid.uuid4().hex}/{name}",
            io.BytesIO(fetched.content),
            filename=name,
            mime_type=fetched.media_type,
            conversation_id=conversation_id,
            source=PREVIEW_SOURCE,
        )
    except (UploadTooLarge, files_db.QuotaExceeded) as exc:
        raise ShowRefused(str(exc)) from exc
    return Card(
        file_id=new_id,
        conversation_id=conversation_id,
        name=name,
        title=_title(title, name),
        type=fetched.media_type,
        kind=fetched.kind.value,
        size=len(fetched.content),
    )


def show(
    db: BridgeLike,
    user: str,
    conversation_id: int,
    *,
    url: Any = None,
    file_id: Any = None,
    title: Any = None,
    permissions: Any = None,
    origins: list[McpOrigin] | None = None,
    fetcher: Callable[..., guard.Fetched] = guard.fetch,
    timeout: float = guard.DEFAULT_TIMEOUT,
) -> Card:
    """Show one document (a link or a file id) in ``conversation_id`` for ``user``.

    ``origins`` defaults to the MCP servers ``permissions`` grant the sender
    (:func:`spark.preview.sources.origins_for`). Raises :class:`ShowRefused`.
    """
    has_url = isinstance(url, str) and url.strip() != ""
    has_file = file_id is not None and file_id != ""
    if has_url == has_file:
        raise ShowRefused("show_document needs either a url or a file_id, not both.")
    if has_file:
        fid = _file_id(file_id)
        if fid is None:
            raise ShowRefused("file_id must be a file id, such as one from list_my_files.")
        return _from_file(db, user, conversation_id, fid, title)
    allowed = origins if origins is not None else sources.origins_for(permissions)
    return _from_link(db, user, conversation_id, str(url), title, allowed, fetcher, timeout)


def result_text(card: Card) -> str:
    """What the tool answers: a sentence for the model, then the card line for the page."""
    label = describe_type(Kind(card.kind))
    shown = "shown in the preview pane" if card.kind != Kind.OTHER.value else "offered"
    return (
        f"{card.name} ({label}, {card.size:,} bytes) is {shown} beside the conversation, "
        f"as file {card.file_id}; every member of the conversation can open it from its "
        "card.\n" + card_line(card)
    )
