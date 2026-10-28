"""Vector embedding storage for conversation context search."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from spark.db.engine import Executor, Row

__all__ = ["add_index_element", "delete_index_elements", "get_index_stats", "search_context"]


async def add_index_element(
    db: Executor,
    conversation_id: int,
    element_type: str,
    content_text: str,
    embedding: np.ndarray[Any, Any],
    user_guid: str,
    *,
    metadata_json: str | None = None,
) -> int | None:
    """Add a context element with embedding. Returns ID, or None if duplicate."""
    content_hash = hashlib.sha256(content_text.encode()).hexdigest()
    embedding_bytes = embedding.astype(np.float32).tobytes()
    async with db.transaction() as conn:
        existing = await conn.fetch_value(
            """SELECT id FROM context_index_elements
                WHERE conversation_id = ? AND content_hash = ?""",
            (conversation_id, content_hash),
        )
        if existing is not None:
            return None
        new_id = await conn.fetch_value(
            """INSERT INTO context_index_elements
                (conversation_id, element_type, content_hash, content_text,
                 embedding, metadata_json, user_guid)
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                conversation_id,
                element_type,
                content_hash,
                content_text,
                embedding_bytes,
                metadata_json,
                user_guid,
            ),
        )
    return int(new_id)


async def search_context(
    db: Executor,
    conversation_ids: list[int],
    query_embedding: np.ndarray[Any, Any],
    *,
    top_k: int = 10,
    threshold: float = 0.3,
    element_type: str | None = None,
) -> list[Row]:
    """Search context elements by cosine similarity across conversations."""
    if not conversation_ids:
        return []
    placeholders = ", ".join("?" for _ in conversation_ids)
    sql = f"SELECT * FROM context_index_elements WHERE conversation_id IN ({placeholders})"
    params: list[Any] = list(conversation_ids)
    if element_type:
        sql += " AND element_type = ?"
        params.append(element_type)
    rows = await db.fetch_all(sql, tuple(params))

    query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    results = []
    for d in rows:
        if d.get("embedding"):
            emb = np.frombuffer(d["embedding"], dtype=np.float32)
            emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
            similarity = float(np.dot(query_norm, emb_norm))
            if similarity >= threshold:
                d["similarity"] = similarity
                del d["embedding"]  # Don't return raw bytes
                results.append(d)

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:top_k]


async def delete_index_elements(db: Executor, conversation_id: int) -> None:
    """Delete all index elements for a conversation."""
    await db.execute(
        "DELETE FROM context_index_elements WHERE conversation_id = ?", (conversation_id,)
    )


async def get_index_stats(db: Executor, conversation_id: int) -> dict[str, int]:
    """Get element count by type for a conversation."""
    rows = await db.fetch_all(
        """SELECT element_type, COUNT(*) as count
            FROM context_index_elements WHERE conversation_id = ?
            GROUP BY element_type""",
        (conversation_id,),
    )
    return {row["element_type"]: int(row["count"]) for row in rows}
