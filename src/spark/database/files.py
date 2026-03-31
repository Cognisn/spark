"""File attachment operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def add_file(
    db: DatabaseConnection,
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
    ph = db.placeholder
    cursor = db.execute(
        f"""INSERT INTO conversation_files
            (conversation_id, filename, file_type, file_size, content_text,
             content_base64, mime_type, token_count, tags, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
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
    db.commit()
    return cursor.lastrowid


def get_conversation_files(db: DatabaseConnection, conversation_id: int) -> list[dict]:
    """Get all files for a conversation."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM conversation_files WHERE conversation_id = {ph} ORDER BY added_at",
        (conversation_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def delete_file(db: DatabaseConnection, file_id: int) -> None:
    """Delete a file attachment."""
    ph = db.placeholder
    db.execute(f"DELETE FROM conversation_files WHERE id = {ph}", (file_id,))
    db.commit()


def delete_conversation_files(db: DatabaseConnection, conversation_id: int) -> None:
    """Delete all files for a conversation."""
    ph = db.placeholder
    db.execute(
        f"DELETE FROM conversation_files WHERE conversation_id = {ph}",
        (conversation_id,),
    )
    db.commit()
