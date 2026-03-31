"""Persistent user memory with semantic search."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from spark.index.embeddings import EmbeddingModel

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"preferences", "facts", "projects", "instructions", "relationships"}


class MemoryIndex:
    """Manages persistent user memories with vector-based semantic search."""

    def __init__(
        self,
        db: DatabaseConnection,
        user_guid: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self._user_guid = user_guid
        config = config or {}
        emb_config = config.get("embedding", {})
        self._embedder = EmbeddingModel(
            model_name=emb_config.get("model", "all-MiniLM-L6-v2"),
            device=emb_config.get("device", "cpu"),
            batch_size=emb_config.get("batch_size", 32),
        )
        auto = config.get("auto_retrieval", {})
        self._auto_enabled = auto.get("enabled", True)
        self._auto_top_k = auto.get("top_k", 5)
        self._auto_threshold = auto.get("threshold", 0.4)

    def store(
        self,
        content: str,
        category: str = "facts",
        *,
        importance: float = 0.5,
        source_conversation_id: int | None = None,
        metadata: str | None = None,
    ) -> int | None:
        """Store a memory. Returns ID or None if duplicate."""
        from spark.database import memories

        if category not in VALID_CATEGORIES:
            category = "facts"

        if memories.memory_exists(self._db, self._user_guid, content):
            return None

        embedding = self._embedder.encode(content)
        return memories.add_memory(
            self._db, self._user_guid, content, category, embedding,
            importance=importance,
            source_conversation_id=source_conversation_id,
            metadata_json=metadata,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        categories: list[str] | None = None,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Search memories by semantic similarity."""
        from spark.database import memories

        query_embedding = self._embedder.encode(query)

        if categories:
            results: list[dict] = []
            for cat in categories:
                if cat in VALID_CATEGORIES:
                    results.extend(
                        memories.search_memories(
                            self._db, self._user_guid, query_embedding,
                            top_k=top_k, threshold=threshold, category=cat,
                        )
                    )
            results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
            results = results[:top_k]
        else:
            results = memories.search_memories(
                self._db, self._user_guid, query_embedding,
                top_k=top_k, threshold=threshold,
            )

        # Update access timestamps
        if results:
            ids = [r["id"] for r in results if "id" in r]
            memories.update_last_accessed(self._db, ids)

        return results

    def auto_retrieve(
        self,
        user_message: str,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> str | None:
        """Auto-retrieve relevant memories for a user message.

        Returns a formatted context block, or None if no matches.
        """
        if not self._auto_enabled:
            return None

        results = self.search(
            user_message,
            top_k=top_k or self._auto_top_k,
            threshold=threshold or self._auto_threshold,
        )

        if not results:
            return None

        lines = ["## Relevant Memories\n"]
        for mem in results:
            cat = mem.get("category", "facts")
            content = mem.get("content", "")
            lines.append(f"- [{cat}] {content}")

        return "\n".join(lines)

    def update(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        importance: float | None = None,
    ) -> bool:
        """Update an existing memory."""
        from spark.database import memories

        mem = memories.get_memory(self._db, memory_id, self._user_guid)
        if mem is None:
            return False

        # Re-embed if content changed
        if content and content != mem.get("content"):
            embedding = self._embedder.encode(content)
            # Direct DB update with new embedding
            import hashlib

            content_hash = hashlib.sha256(content.encode()).hexdigest()
            ph = self._db.placeholder
            sets = [f"content = {ph}", f"content_hash = {ph}", f"embedding = {ph}"]
            params: list[Any] = [content, content_hash, embedding.astype("float32").tobytes()]

            if category:
                sets.append(f"category = {ph}")
                params.append(category)
            if importance is not None:
                sets.append(f"importance = {ph}")
                params.append(importance)

            params.extend([memory_id, self._user_guid])
            self._db.execute(
                f"UPDATE user_memories SET {', '.join(sets)} WHERE id = {ph} AND user_guid = {ph}",
                tuple(params),
            )
            self._db.commit()
        else:
            updates: dict[str, Any] = {}
            if category:
                updates["category"] = category
            if importance is not None:
                updates["importance"] = importance
            if updates:
                ph = self._db.placeholder
                sets_sql = ", ".join(f"{k} = {ph}" for k in updates)
                params_list = list(updates.values()) + [memory_id, self._user_guid]
                self._db.execute(
                    f"UPDATE user_memories SET {sets_sql} WHERE id = {ph} AND user_guid = {ph}",
                    tuple(params_list),
                )
                self._db.commit()

        return True

    def delete(self, memory_id: int) -> bool:
        """Delete a memory."""
        from spark.database import memories

        mem = memories.get_memory(self._db, memory_id, self._user_guid)
        if mem is None:
            return False
        memories.delete_memory(self._db, memory_id, self._user_guid)
        return True

    def list_all(
        self, *, category: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List all memories, optionally filtered by category."""
        from spark.database import memories

        all_mems = memories.get_all_memories(self._db, self._user_guid, limit=limit)
        if category:
            return [m for m in all_mems if m.get("category") == category]
        return all_mems

    def get(self, memory_id: int) -> dict[str, Any] | None:
        """Get a single memory."""
        from spark.database import memories

        return memories.get_memory(self._db, memory_id, self._user_guid)

    def clear_all(self) -> None:
        """Delete all memories for this user."""
        from spark.database import memories

        memories.delete_all_memories(self._db, self._user_guid)
