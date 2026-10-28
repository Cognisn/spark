"""User memory storage with vector embeddings."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import numpy as np

from spark.db.engine import Executor, Row

__all__ = [
    "add_memory",
    "delete_all_memories",
    "delete_memory",
    "get_all_memories",
    "get_memory",
    "memory_exists",
    "search_memories",
    "update_last_accessed",
]


async def add_memory(
    db: Executor,
    user_guid: str,
    content: str,
    category: str,
    embedding: np.ndarray[Any, Any],
    *,
    importance: float = 0.5,
    source_conversation_id: int | None = None,
    metadata_json: str | None = None,
) -> int | None:
    """Add a memory. Returns ID, or None if the user already has the same content."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    embedding_bytes = embedding.astype(np.float32).tobytes()
    new_id = await db.fetch_value(
        """INSERT INTO user_memories
            (user_guid, content, category, content_hash, embedding, importance,
             source_conversation_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_guid, content_hash) DO NOTHING
            RETURNING id""",
        (
            user_guid,
            content,
            category,
            content_hash,
            embedding_bytes,
            importance,
            source_conversation_id,
            metadata_json,
        ),
    )
    return int(new_id) if new_id is not None else None


async def get_memory(db: Executor, memory_id: int, user_guid: str) -> Row | None:
    """Get a single memory by ID."""
    result = await db.fetch_one(
        "SELECT * FROM user_memories WHERE id = ? AND user_guid = ?",
        (memory_id, user_guid),
    )
    if result is None:
        return None
    if result.get("embedding"):
        result["embedding"] = np.frombuffer(result["embedding"], dtype=np.float32)
    return result


async def search_memories(
    db: Executor,
    user_guid: str,
    query_embedding: np.ndarray[Any, Any],
    *,
    top_k: int = 10,
    threshold: float = 0.3,
    category: str | None = None,
) -> list[Row]:
    """Search memories by cosine similarity."""
    if category:
        rows = await db.fetch_all(
            "SELECT * FROM user_memories WHERE user_guid = ? AND category = ?",
            (user_guid, category),
        )
    else:
        rows = await db.fetch_all("SELECT * FROM user_memories WHERE user_guid = ?", (user_guid,))

    query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    results = []
    for d in rows:
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


async def delete_memory(db: Executor, memory_id: int, user_guid: str) -> None:
    """Delete a memory."""
    await db.execute(
        "DELETE FROM user_memories WHERE id = ? AND user_guid = ?", (memory_id, user_guid)
    )


async def delete_all_memories(db: Executor, user_guid: str) -> None:
    """Delete all memories for a user."""
    await db.execute("DELETE FROM user_memories WHERE user_guid = ?", (user_guid,))


async def get_all_memories(
    db: Executor, user_guid: str, *, limit: int = 100, offset: int = 0
) -> list[Row]:
    """Get all memories for a user with pagination."""
    return await db.fetch_all(
        """SELECT id, user_guid, content, category, importance, created_at,
                last_accessed, source_conversation_id, metadata_json
            FROM user_memories WHERE user_guid = ?
            ORDER BY importance DESC, created_at DESC
            LIMIT ? OFFSET ?""",
        (user_guid, limit, offset),
    )


async def memory_exists(db: Executor, user_guid: str, content: str) -> bool:
    """Check if a memory with the same content already exists."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    found = await db.fetch_value(
        "SELECT 1 FROM user_memories WHERE user_guid = ? AND content_hash = ?",
        (user_guid, content_hash),
    )
    return found is not None


async def update_last_accessed(db: Executor, memory_ids: list[int]) -> None:
    """Update last_accessed timestamp for retrieved memories."""
    if not memory_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ", ".join("?" for _ in memory_ids)
    await db.execute(
        f"UPDATE user_memories SET last_accessed = ? WHERE id IN ({placeholders})",
        (now, *memory_ids),
    )
