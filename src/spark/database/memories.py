"""User memory storage with vector embeddings."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def add_memory(
    db: DatabaseConnection,
    user_guid: str,
    content: str,
    category: str,
    embedding: np.ndarray,
    *,
    importance: float = 0.5,
    source_conversation_id: int | None = None,
    metadata_json: str | None = None,
) -> int | None:
    """Add a memory. Returns ID, or None if duplicate."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    ph = db.placeholder
    embedding_bytes = embedding.astype(np.float32).tobytes()

    try:
        cursor = db.execute(
            f"""INSERT INTO user_memories
                (user_guid, content, category, content_hash, embedding, importance,
                 source_conversation_id, metadata_json)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
            (
                user_guid, content, category, content_hash, embedding_bytes,
                importance, source_conversation_id, metadata_json,
            ),
        )
        db.commit()
        return cursor.lastrowid
    except Exception:
        # Duplicate content_hash
        db.rollback()
        return None


def get_memory(db: DatabaseConnection, memory_id: int, user_guid: str) -> dict | None:
    """Get a single memory by ID."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM user_memories WHERE id = {ph} AND user_guid = {ph}",
        (memory_id, user_guid),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    result = dict(row)
    if result.get("embedding"):
        result["embedding"] = np.frombuffer(result["embedding"], dtype=np.float32)
    return result


def search_memories(
    db: DatabaseConnection,
    user_guid: str,
    query_embedding: np.ndarray,
    *,
    top_k: int = 10,
    threshold: float = 0.3,
    category: str | None = None,
) -> list[dict]:
    """Search memories by cosine similarity."""
    ph = db.placeholder
    if category:
        cursor = db.execute(
            f"SELECT * FROM user_memories WHERE user_guid = {ph} AND category = {ph}",
            (user_guid, category),
        )
    else:
        cursor = db.execute(
            f"SELECT * FROM user_memories WHERE user_guid = {ph}",
            (user_guid,),
        )

    query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    results = []
    for row in cursor.fetchall():
        d = dict(row)
        if d.get("embedding"):
            emb = np.frombuffer(d["embedding"], dtype=np.float32)
            emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
            similarity = float(np.dot(query_norm, emb_norm))
            if similarity >= threshold:
                d["similarity"] = similarity
                d["embedding"] = emb
                results.append(d)

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:top_k]


def delete_memory(db: DatabaseConnection, memory_id: int, user_guid: str) -> None:
    """Delete a memory."""
    ph = db.placeholder
    db.execute(
        f"DELETE FROM user_memories WHERE id = {ph} AND user_guid = {ph}",
        (memory_id, user_guid),
    )
    db.commit()


def delete_all_memories(db: DatabaseConnection, user_guid: str) -> None:
    """Delete all memories for a user."""
    ph = db.placeholder
    db.execute(f"DELETE FROM user_memories WHERE user_guid = {ph}", (user_guid,))
    db.commit()


def get_all_memories(
    db: DatabaseConnection, user_guid: str, *, limit: int = 100, offset: int = 0
) -> list[dict]:
    """Get all memories for a user with pagination."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT id, user_guid, content, category, importance, created_at,
                last_accessed, source_conversation_id, metadata_json
            FROM user_memories WHERE user_guid = {ph}
            ORDER BY importance DESC, created_at DESC
            LIMIT {ph} OFFSET {ph}""",
        (user_guid, limit, offset),
    )
    return [dict(row) for row in cursor.fetchall()]


def memory_exists(db: DatabaseConnection, user_guid: str, content: str) -> bool:
    """Check if a memory with the same content already exists."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT 1 FROM user_memories WHERE user_guid = {ph} AND content_hash = {ph}",
        (user_guid, content_hash),
    )
    return cursor.fetchone() is not None


def update_last_accessed(db: DatabaseConnection, memory_ids: list[int]) -> None:
    """Update last_accessed timestamp for retrieved memories."""
    if not memory_ids:
        return
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ", ".join([ph] * len(memory_ids))
    db.execute(
        f"UPDATE user_memories SET last_accessed = {ph} WHERE id IN ({placeholders})",
        (now, *memory_ids),
    )
    db.commit()
