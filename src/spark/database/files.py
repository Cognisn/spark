"""File attachment operations."""

from __future__ import annotations

from spark.db.engine import Executor, Row

__all__ = ["add_file", "delete_conversation_files", "delete_file", "get_conversation_files"]


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
