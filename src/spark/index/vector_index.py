"""Conversation vector index for semantic search over chat history."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from spark.index.embeddings import EmbeddingModel

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

VALID_TYPES = {"system", "user", "assistant", "tool_call", "tool_result"}
_TOOL_RESULTS_MARKER = "[TOOL_RESULTS]"


class ConversationVectorIndex:
    """Semantic search over conversation messages, tool calls, and results."""

    def __init__(
        self,
        db: DatabaseConnection,
        conversation_id: int,
        user_guid: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self._conversation_id = conversation_id
        self._user_guid = user_guid
        config = config or {}
        emb_config = config.get("embedding", {})
        idx_config = config.get("indexing", {})

        self._embedder = EmbeddingModel(
            model_name=emb_config.get("model", "all-MiniLM-L6-v2"),
            device=emb_config.get("device", "cpu"),
        )
        self._chunk_size = idx_config.get("chunk_size", 500)
        self._chunk_overlap = idx_config.get("chunk_overlap", 50)

        self._cache: list[dict[str, Any]] | None = None
        self._cache_dirty = True

    def index_element(
        self,
        element_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        """Index a content element. Returns ID or None if duplicate."""
        from spark.database import context_index

        if element_type not in VALID_TYPES:
            return None
        content = content.strip()
        if not content:
            return None

        embedding = self._embedder.encode(content)
        metadata_json = json.dumps(metadata) if metadata else None

        result = context_index.add_index_element(
            self._db,
            self._conversation_id,
            element_type,
            content,
            embedding,
            self._user_guid,
            metadata_json=metadata_json,
        )
        if result is not None:
            self._cache_dirty = True
        return result

    def index_message(
        self,
        message_id: int,
        role: str,
        content: str,
        timestamp: str | None = None,
    ) -> list[int]:
        """Index a message. Returns list of element IDs."""
        if content.startswith(_TOOL_RESULTS_MARKER):
            return []

        metadata = {"message_id": message_id}
        if timestamp:
            metadata["timestamp"] = timestamp

        # Handle JSON content blocks
        if content.startswith("["):
            try:
                blocks = json.loads(content)
                if isinstance(blocks, list):
                    ids = []
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            eid = self.index_element(role, block["text"], metadata)
                            if eid:
                                ids.append(eid)
                    return ids
            except json.JSONDecodeError:
                pass

        eid = self.index_element(role, content, metadata)
        return [eid] if eid else []

    def index_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        message_id: int | None = None,
    ) -> int | None:
        """Index a tool call."""
        content = f"Tool: {tool_name}\nInput: {json.dumps(tool_input)}"
        metadata: dict[str, Any] = {"tool_name": tool_name}
        if message_id:
            metadata["message_id"] = message_id
        return self.index_element("tool_call", content, metadata)

    def index_tool_result(
        self,
        tool_name: str,
        result: str,
        message_id: int | None = None,
        *,
        is_error: bool = False,
    ) -> int | None:
        """Index a tool result."""
        # Truncate large results
        if len(result) > 2000:
            result = result[:2000] + "... [truncated]"
        content = f"Tool result ({tool_name}): {result}"
        metadata: dict[str, Any] = {"tool_name": tool_name, "is_error": is_error}
        if message_id:
            metadata["message_id"] = message_id
        return self.index_element("tool_result", content, metadata)

    def index_system_instructions(self, instructions: str) -> list[int]:
        """Index system instructions, chunking if long."""
        chunks = self._chunk_text(instructions)
        ids = []
        for i, chunk in enumerate(chunks):
            metadata = {"chunk_index": i, "total_chunks": len(chunks)}
            eid = self.index_element("system", chunk, metadata)
            if eid:
                ids.append(eid)
        return ids

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        element_types: list[str] | None = None,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Search this conversation's index."""
        from spark.database import context_index

        query_embedding = self._embedder.encode(query)
        return context_index.search_context(
            self._db,
            [self._conversation_id],
            query_embedding,
            top_k=top_k,
            threshold=threshold,
            element_type=element_types[0] if element_types and len(element_types) == 1 else None,
        )

    def search_multi(
        self,
        query: str,
        conversation_ids: list[int],
        *,
        top_k: int = 10,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Search across multiple conversations."""
        from spark.database import context_index

        query_embedding = self._embedder.encode(query)
        return context_index.search_context(
            self._db,
            conversation_ids,
            query_embedding,
            top_k=top_k,
            threshold=threshold,
        )

    def clear(self) -> None:
        """Clear all index elements for this conversation."""
        from spark.database import context_index

        context_index.delete_index_elements(self._db, self._conversation_id)
        self._cache_dirty = True
        self._cache = None

    def get_stats(self) -> dict[str, int]:
        """Get element counts by type."""
        from spark.database import context_index

        return context_index.get_index_stats(self._db, self._conversation_id)

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks."""
        if len(text) <= self._chunk_size:
            return [text]

        chunks: list[str] = []
        pos = 0
        while pos < len(text):
            end = min(pos + self._chunk_size, len(text))
            chunk = text[pos:end]

            # Try to break at sentence/word boundary
            if end < len(text):
                for sep in [". ", ".\n", "\n\n", "\n", " "]:
                    last = chunk.rfind(sep)
                    if last > self._chunk_size // 2:
                        chunk = chunk[: last + len(sep)]
                        break

            chunks.append(chunk.strip())
            new_pos = pos + len(chunk) - self._chunk_overlap
            if new_pos <= pos:
                new_pos = pos + max(1, len(chunk))
            pos = new_pos

        return [c for c in chunks if c]
