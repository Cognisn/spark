"""File attachment operations and per-user file area metadata.

``conversation_files`` holds the 0.2.0 inline attachments. ``user_files``
describes files in a user's area (uploads, documents SPARK creates, extracted
archive members) and ``user_file_usage`` is the counter quotas are checked
against. Recording a file and charging its size happen in one transaction that
first locks the user's counter row, so parallel uploads cannot overrun a quota.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from spark.database.scope import (
    OWNED_CONVERSATIONS,
    READABLE_CONVERSATIONS,
    Capability,
    require_access,
)
from spark.db.engine import Connection, Executor, Row

__all__ = [
    "NewUserFile",
    "QuotaExceeded",
    "add_file",
    "artefact_origins",
    "artefact_titles",
    "delete_conversation_files",
    "delete_file",
    "delete_user_file",
    "get_conversation_file",
    "get_conversation_files",
    "get_file_title",
    "get_readable_file",
    "get_shared_attachment",
    "get_usage",
    "get_user_file",
    "get_user_file_by_key",
    "list_conversation_artefacts",
    "list_user_files",
    "record_user_file",
    "record_user_files",
    "set_conversation_file_title",
    "set_file_title",
]


class QuotaExceeded(Exception):
    """Recording the file would take the user over their storage quota."""

    def __init__(self, used: int, requested: int, quota: int) -> None:
        super().__init__(
            f"Storage quota exceeded: {used:,} of {quota:,} bytes are in use and "
            f"{requested:,} more were requested."
        )
        self.used = used
        self.requested = requested
        self.quota = quota


@dataclass(frozen=True)
class NewUserFile:
    """A file to record in a user's area."""

    key: str
    filename: str
    size: int
    mime_type: str | None = None
    conversation_id: int | None = None
    source: str = "upload"
    # The artefact's title and kind (PC5); None takes the file name and the source's kind.
    title: str | None = None
    kind: str | None = None

    def artefact(self) -> tuple[str, str]:
        """The title and kind to store: given, or from the file name and the source.

        Raises ``ValueError`` for a kind outside :data:`spark.files.titles.KINDS`.
        """
        from spark.files.titles import KINDS, TITLE_LIMIT, clean_text, kind_for_source

        kind = self.kind if self.kind is not None else kind_for_source(self.source)
        if kind not in KINDS:
            raise ValueError(f"A file's kind is one of: {', '.join(KINDS)}.")
        title = clean_text(self.title, limit=TITLE_LIMIT) if self.title is not None else ""
        return title or clean_text(self.filename, limit=TITLE_LIMIT) or "file", kind


async def _lock_usage(conn: Connection, user_guid: str) -> int:
    """Create and row-lock the user's counter; return the bytes in use."""
    await conn.execute(
        "INSERT INTO user_file_usage (user_guid, bytes_used) VALUES (?, 0) "
        "ON CONFLICT (user_guid) DO NOTHING",
        (user_guid,),
    )
    # A no-op update takes the row lock on PostgreSQL; SQLite already holds the
    # write lock from BEGIN IMMEDIATE.
    await conn.execute(
        "UPDATE user_file_usage SET bytes_used = bytes_used WHERE user_guid = ?", (user_guid,)
    )
    return int(
        await conn.fetch_value(
            "SELECT bytes_used FROM user_file_usage WHERE user_guid = ?", (user_guid,)
        )
    )


async def record_user_files(
    db: Executor, user_guid: str, new_files: Sequence[NewUserFile], *, quota_bytes: int | None
) -> list[int]:
    """Record files in the user's area and charge their sizes, all or nothing.

    A key already recorded is replaced and charged only the difference in size.
    ``quota_bytes`` of None means no quota. Raises :class:`QuotaExceeded`.
    """
    artefacts = [new.artefact() for new in new_files]
    async with db.transaction() as conn:
        used = await _lock_usage(conn, user_guid)
        delta = 0
        for new in new_files:
            old = await conn.fetch_value(
                "SELECT size_bytes FROM user_files WHERE user_guid = ? AND storage_key = ?",
                (user_guid, new.key),
            )
            delta += new.size - int(old or 0)
        if quota_bytes is not None and delta > 0 and used + delta > quota_bytes:
            raise QuotaExceeded(used, delta, quota_bytes)
        await conn.execute(
            "UPDATE user_file_usage SET bytes_used = bytes_used + ? WHERE user_guid = ?",
            (delta, user_guid),
        )
        ids: list[int] = []
        for new, (title, kind) in zip(new_files, artefacts, strict=True):
            new_id = await conn.fetch_value(
                """INSERT INTO user_files
                    (user_guid, storage_key, filename, size_bytes, mime_type,
                     conversation_id, source, title, kind)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_guid, storage_key) DO UPDATE SET
                        filename = excluded.filename,
                        size_bytes = excluded.size_bytes,
                        mime_type = excluded.mime_type,
                        conversation_id = excluded.conversation_id,
                        source = excluded.source,
                        title = excluded.title,
                        kind = excluded.kind,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id""",
                (
                    user_guid,
                    new.key,
                    new.filename,
                    new.size,
                    new.mime_type,
                    new.conversation_id,
                    new.source,
                    title,
                    kind,
                ),
            )
            ids.append(int(new_id))
        return ids


async def record_user_file(
    db: Executor, user_guid: str, new: NewUserFile, *, quota_bytes: int | None
) -> int:
    """Record one file (see :func:`record_user_files`); return its id."""
    return (await record_user_files(db, user_guid, [new], quota_bytes=quota_bytes))[0]


async def list_user_files(db: Executor, user_guid: str) -> list[Row]:
    """The user's files, newest first."""
    return await db.fetch_all(
        "SELECT * FROM user_files WHERE user_guid = ? ORDER BY updated_at DESC, id DESC",
        (user_guid,),
    )


async def get_user_file(db: Executor, user_guid: str, file_id: int) -> Row | None:
    """One of the user's files by id, or None (including another user's file)."""
    return await db.fetch_one(
        "SELECT * FROM user_files WHERE id = ? AND user_guid = ?", (file_id, user_guid)
    )


async def get_shared_attachment(db: Executor, user_guid: str, file_id: int) -> Row | None:
    """Another member's file ``user_guid`` may download because it is attached in a
    conversation they may read, or None.

    A file is attached there when it was uploaded into the conversation, or when a
    message its owner sent in the conversation names it (the attachment note's
    ``- file <id>: <name>`` line). Nothing else of the owner's file area is reachable.
    """
    return await db.fetch_one(
        f"""SELECT f.* FROM user_files f WHERE f.id = ? AND (
                f.conversation_id IN ({READABLE_CONVERSATIONS})
                OR EXISTS (SELECT 1 FROM messages m
                    WHERE m.user_guid = f.user_guid AND m.role = 'user'
                    AND m.conversation_id IN ({READABLE_CONVERSATIONS})
                    AND m.content LIKE ?))""",
        (file_id, user_guid, user_guid, f"%- file {int(file_id)}: %"),
    )


async def get_conversation_file(
    db: Executor, user_guid: str, conversation_id: int, file_id: int
) -> Row | None:
    """A file of ``conversation_id`` (uploaded into it, created in it or shown in it),
    whoever stored it, when ``user_guid`` may read the conversation; or None.

    The document preview opens files only this way, so every member of a shared
    conversation reaches its files and nobody else does.
    """
    return await db.fetch_one(
        f"""SELECT * FROM user_files WHERE id = ? AND conversation_id = ?
            AND conversation_id IN ({READABLE_CONVERSATIONS})""",
        (file_id, conversation_id, user_guid),
    )


async def get_readable_file(db: Executor, user_guid: str, file_id: int) -> Row | None:
    """A file ``user_guid`` may open: one of their own, or a file of a conversation they
    may read (uploaded into it, created in it or shown in it, by any member); or None.

    The chart API opens artefacts this way, then asks the conversation's access
    decision; nothing else of another person's area is reachable.
    """
    return await db.fetch_one(
        f"""SELECT * FROM user_files WHERE id = ? AND (user_guid = ?
            OR conversation_id IN ({READABLE_CONVERSATIONS}))""",
        (file_id, user_guid, user_guid),
    )


async def list_conversation_artefacts(
    db: Executor, user_guid: str, conversation_id: int
) -> list[Row]:
    """Every member's files of a conversation ``user_guid`` may read, oldest first, with
    their titles and kinds (empty when they may not read it)."""
    return await db.fetch_all(
        f"""SELECT * FROM user_files WHERE conversation_id = ?
            AND conversation_id IN ({READABLE_CONVERSATIONS}) ORDER BY created_at, id""",
        (conversation_id, user_guid),
    )


# A file id named in a message: a card line's ``"file_id": 12`` (inside a tool result the
# quotes are escaped), a created file's "kept in your files as file 12", or an attachment
# note's "- file 12: name".
_FILE_MENTION = re.compile(r'file_id\\*"?\s*:\s*(\d+)|\bfile (\d+)(?!\d)')


async def artefact_origins(
    db: Executor, user_guid: str, conversation_id: int, file_ids: Iterable[int]
) -> dict[int, int]:
    """The id of the first message of the conversation that names each file (the message
    that produced or attached it), for the files that any message names.

    Only messages of a conversation ``user_guid`` may read are looked at; the result has
    no entry for a file no message names.
    """
    wanted = {int(i) for i in file_ids}
    if not wanted:
        return {}
    rows = await db.fetch_all(
        f"""SELECT id, content FROM messages WHERE conversation_id = ?
            AND conversation_id IN ({READABLE_CONVERSATIONS}) AND content LIKE '%file%'
            ORDER BY timestamp, id""",
        (conversation_id, user_guid),
    )
    found: dict[int, int] = {}
    for row in rows:
        for match in _FILE_MENTION.finditer(str(row["content"] or "")):
            number = int(match.group(1) or match.group(2))
            if number in wanted and number not in found:
                found[number] = int(row["id"])
    return found


async def artefact_titles(db: Executor, file_ids: Iterable[int]) -> dict[int, tuple[str, str]]:
    """``{file id: (title, kind)}`` for the files named.

    Unscoped: the caller has already decided that the person may see each file (a
    resource a conversation inherits); it is never reachable from a request on its own.
    """
    ids = sorted({int(i) for i in file_ids})
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT id, title, kind FROM user_files WHERE id IN ({marks})", tuple(ids)
    )
    return {int(r["id"]): (str(r["title"]), str(r["kind"])) for r in rows}


async def get_file_title(db: Executor, user_guid: str, file_id: int) -> str | None:
    """The title of one of the user's own files, or None."""
    value = await db.fetch_value(
        "SELECT title FROM user_files WHERE id = ? AND user_guid = ?", (file_id, user_guid)
    )
    return None if value is None else str(value)


async def set_file_title(db: Executor, user_guid: str, file_id: int, title: str) -> Row | None:
    """Rename one of the user's own files; return the row, or None for another's.

    Raises :class:`spark.files.titles.TitleRefused` (a ``ValueError``) for a blank or
    over-long title.
    """
    from spark.files.titles import require_title

    cleaned = require_title(title)
    return await db.fetch_one(
        """UPDATE user_files SET title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_guid = ? RETURNING *""",
        (cleaned, file_id, user_guid),
    )


async def set_conversation_file_title(
    db: Executor, user_guid: str, conversation_id: int, file_id: int, title: str
) -> Row | None:
    """Rename a file of a conversation, whoever stored it, as its owner or a contributor.

    Returns the row, or None when the file is not one of the conversation's. Raises
    :class:`spark.database.scope.NotOwned` for a stranger,
    :class:`spark.database.scope.NotPermitted` for a viewer and
    :class:`spark.files.titles.TitleRefused` for a blank or over-long title.
    """
    from spark.files.titles import require_title

    cleaned = require_title(title)
    async with db.transaction() as conn:
        await require_access(conn, user_guid, conversation_id, Capability.CONTRIBUTE)
        return await conn.fetch_one(
            """UPDATE user_files SET title = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND conversation_id = ? RETURNING *""",
            (cleaned, file_id, conversation_id),
        )


async def get_user_file_by_key(db: Executor, user_guid: str, key: str) -> Row | None:
    """One of the user's files by storage key, or None."""
    return await db.fetch_one(
        "SELECT * FROM user_files WHERE user_guid = ? AND storage_key = ?", (user_guid, key)
    )


async def delete_user_file(db: Executor, user_guid: str, file_id: int) -> Row | None:
    """Remove one of the user's files and release its size; return the removed row."""
    async with db.transaction() as conn:
        await _lock_usage(conn, user_guid)
        row = await conn.fetch_one(
            "DELETE FROM user_files WHERE id = ? AND user_guid = ? RETURNING *",
            (file_id, user_guid),
        )
        if row is None:
            return None
        await conn.execute(
            "UPDATE user_file_usage SET bytes_used = bytes_used - ? WHERE user_guid = ?",
            (int(row["size_bytes"]), user_guid),
        )
        return row


async def get_usage(db: Executor, user_guid: str) -> int:
    """Bytes charged to the user's area."""
    value = await db.fetch_value(
        "SELECT bytes_used FROM user_file_usage WHERE user_guid = ?", (user_guid,)
    )
    return int(value or 0)


async def add_file(
    db: Executor,
    conversation_id: int,
    filename: str,
    file_type: str,
    file_size: int,
    user_guid: str,
    *,
    content_text: str | None = None,
    content_base64: str | None = None,
    mime_type: str | None = None,
    token_count: int = 0,
    tags: str | None = None,
) -> int:
    """Attach a file to a conversation the user may send in. Returns file ID.

    Raises :class:`spark.database.scope.NotOwned` when the user is not a member, and
    :class:`spark.database.scope.NotPermitted` when they may only read.
    """
    async with db.transaction() as conn:
        await require_access(conn, user_guid, conversation_id, Capability.CONTRIBUTE)
        return await _insert_file(
            conn,
            conversation_id,
            filename,
            file_type,
            file_size,
            user_guid,
            content_text,
            content_base64,
            mime_type,
            token_count,
            tags,
        )


async def _insert_file(
    db: Executor,
    conversation_id: int,
    filename: str,
    file_type: str,
    file_size: int,
    user_guid: str,
    content_text: str | None,
    content_base64: str | None,
    mime_type: str | None,
    token_count: int,
    tags: str | None,
) -> int:
    new_id = await db.fetch_value(
        """INSERT INTO conversation_files
            (conversation_id, filename, file_type, file_size, content_text,
             content_base64, mime_type, token_count, tags, user_guid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            conversation_id,
            filename,
            file_type,
            file_size,
            content_text,
            content_base64,
            mime_type,
            token_count,
            tags,
            user_guid,
        ),
    )
    return int(new_id)


async def get_conversation_files(db: Executor, user_guid: str, conversation_id: int) -> list[Row]:
    """All files of a conversation the user may read (attachments are visible to every
    member)."""
    return await db.fetch_all(
        f"""SELECT * FROM conversation_files WHERE conversation_id = ?
            AND conversation_id IN ({READABLE_CONVERSATIONS}) ORDER BY added_at""",
        (conversation_id, user_guid),
    )


async def delete_file(db: Executor, user_guid: str, file_id: int) -> None:
    """Delete a file attachment of a conversation the user owns."""
    await db.execute(
        f"""DELETE FROM conversation_files WHERE id = ?
            AND conversation_id IN ({OWNED_CONVERSATIONS})""",
        (file_id, user_guid),
    )


async def delete_conversation_files(db: Executor, user_guid: str, conversation_id: int) -> None:
    """Delete all files of a conversation the user owns."""
    await db.execute(
        f"""DELETE FROM conversation_files WHERE conversation_id = ?
            AND conversation_id IN ({OWNED_CONVERSATIONS})""",
        (conversation_id, user_guid),
    )
