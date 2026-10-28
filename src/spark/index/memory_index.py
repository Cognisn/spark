"""Persistent user memory with semantic search."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from spark.index.embeddings import EmbeddingModel

if TYPE_CHECKING:
    from spark.db.bridge import SyncDatabase

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"preferences", "facts", "projects", "instructions", "relationships"}


class MemoryIndex:
    """Manages persistent user memories with vector-based semantic search."""

    def __init__(
        self,
        db: SyncDatabase,
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

        if self._db.call(memories.memory_exists, self._user_guid, content):
            return None

        embedding = self._embedder.encode(content)
        return self._db.call(
            memories.add_memory,
            self._user_guid,
            content,
            category,
            embedding,
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
                        self._db.call(
                            memories.search_memories,
                            self._user_guid,
                            query_embedding,
                            top_k=top_k,
                            threshold=threshold,
                            category=cat,
                        )
                    )
            results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
            results = results[:top_k]
        else:
            results = self._db.call(
                memories.search_memories,
                self._user_guid,
                query_embedding,
                top_k=top_k,
                threshold=threshold,
            )

        # Update access timestamps
        if results:
            ids = [r["id"] for r in results if "id" in r]
            self._db.call(memories.update_last_accessed, ids)

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

        mem = self._db.call(memories.get_memory, memory_id, self._user_guid)
        if mem is None:
            return False

        # Re-embed if content changed
        new_content = content if content and content != mem.get("content") else None
        self._db.call(
            memories.update_memory,
            memory_id,
            self._user_guid,
            content=new_content,
            embedding=self._embedder.encode(new_content) if new_content else None,
            category=category,
            importance=importance,
        )
        return True

    def delete(self, memory_id: int) -> bool:
        """Delete a memory."""
        from spark.database import memories

        mem = self._db.call(memories.get_memory, memory_id, self._user_guid)
        if mem is None:
            return False
        self._db.call(memories.delete_memory, memory_id, self._user_guid)
        return True

    def list_all(self, *, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List all memories, optionally filtered by category."""
        from spark.database import memories

        all_mems = self._db.call(memories.get_all_memories, self._user_guid, limit=limit)
        if category:
            return [m for m in all_mems if m.get("category") == category]
        return all_mems

    def get(self, memory_id: int) -> dict[str, Any] | None:
        """Get a single memory."""
        from spark.database import memories

        return self._db.call(memories.get_memory, memory_id, self._user_guid)

    def clear_all(self) -> None:
        """Delete all memories for this user."""
        from spark.database import memories

        self._db.call(memories.delete_all_memories, self._user_guid)
