"""File attachment operations and per-user file area metadata.

``conversation_files`` holds the 0.2.0 inline attachments. ``user_files``
describes files in a user's area (uploads, documents SPARK creates, extracted
archive members) and ``user_file_usage`` is the counter quotas are checked
against. Recording a file and charging its size happen in one transaction that
first locks the user's counter row, so parallel uploads cannot overrun a quota.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from spark.db.engine import Connection, Executor, Row

__all__ = [
    "NewUserFile",
    "QuotaExceeded",
    "add_file",
    "delete_conversation_files",
    "delete_file",
    "delete_user_file",
    "get_conversation_files",
    "get_usage",
    "get_user_file",
    "get_user_file_by_key",
    "list_user_files",
    "record_user_file",
    "record_user_files",
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
        for new in new_files:
            new_id = await conn.fetch_value(
                """INSERT INTO user_files
                    (user_guid, storage_key, filename, size_bytes, mime_type,
                     conversation_id, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_guid, storage_key) DO UPDATE SET
                        filename = excluded.filename,
                        size_bytes = excluded.size_bytes,
                        mime_type = excluded.mime_type,
                        conversation_id = excluded.conversation_id,
                        source = excluded.source,
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
    """Attach a file to a conversation. Returns file ID."""
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


async def get_conversation_files(db: Executor, conversation_id: int) -> list[Row]:
    """Get all files for a conversation."""
    return await db.fetch_all(
        "SELECT * FROM conversation_files WHERE conversation_id = ? ORDER BY added_at",
        (conversation_id,),
    )


async def delete_file(db: Executor, file_id: int) -> None:
    """Delete a file attachment."""
    await db.execute("DELETE FROM conversation_files WHERE id = ?", (file_id,))


async def delete_conversation_files(db: Executor, conversation_id: int) -> None:
    """Delete all files for a conversation."""
    await db.execute("DELETE FROM conversation_files WHERE conversation_id = ?", (conversation_id,))
