"""Vector embedding storage for conversation context search."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def add_index_element(
    db: DatabaseConnection,
    conversation_id: int,
    element_type: str,
    content_text: str,
    embedding: np.ndarray,
    user_guid: str,
    *,
    metadata_json: str | None = None,
) -> int | None:
    """Add a context element with embedding. Returns ID, or None if duplicate."""
    content_hash = hashlib.sha256(content_text.encode()).hexdigest()
    ph = db.placeholder

    # Check for duplicate
    cursor = db.execute(
        f"""SELECT id FROM context_index_elements
            WHERE conversation_id = {ph} AND content_hash = {ph}""",
        (conversation_id, content_hash),
    )
    if cursor.fetchone():
        return None

    embedding_bytes = embedding.astype(np.float32).tobytes()
    cursor = db.execute(
        f"""INSERT INTO context_index_elements
            (conversation_id, element_type, content_hash, content_text,
             embedding, metadata_json, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
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
    db.commit()
    return cursor.lastrowid


def search_context(
    db: DatabaseConnection,
    conversation_ids: list[int],
    query_embedding: np.ndarray,
    *,
    top_k: int = 10,
    threshold: float = 0.3,
    element_type: str | None = None,
) -> list[dict]:
    """Search context elements by cosine similarity across conversations."""
    if not conversation_ids:
        return []

    ph = db.placeholder
    placeholders = ", ".join([ph] * len(conversation_ids))
    sql = f"SELECT * FROM context_index_elements WHERE conversation_id IN ({placeholders})"
    params: list = list(conversation_ids)

    if element_type:
        sql += f" AND element_type = {ph}"
        params.append(element_type)

    cursor = db.execute(sql, tuple(params))

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
                del d["embedding"]  # Don't return raw bytes
                results.append(d)

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:top_k]


def delete_index_elements(db: DatabaseConnection, conversation_id: int) -> None:
    """Delete all index elements for a conversation."""
    ph = db.placeholder
    db.execute(
        f"DELETE FROM context_index_elements WHERE conversation_id = {ph}",
        (conversation_id,),
    )
    db.commit()


def get_index_stats(db: DatabaseConnection, conversation_id: int) -> dict:
    """Get element count by type for a conversation."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT element_type, COUNT(*) as count
            FROM context_index_elements WHERE conversation_id = {ph}
            GROUP BY element_type""",
        (conversation_id,),
    )
    return {row[0]: row[1] for row in cursor.fetchall()}
