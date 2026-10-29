"""Conversation vector index for semantic search over chat history.

Vectors come from an :class:`~spark.index.vectors.EmbeddingModel` and live in the
mode's :class:`~spark.index.vectors.VectorStore`; the index runs on worker
threads and reaches the database through the bridge.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from spark.index.embeddings import SentenceTransformerEmbedding
from spark.index.vectors import CONTEXT, EmbeddingModel, Vector, VectorStore, vector_store

if TYPE_CHECKING:
    from spark.db.bridge import SyncDatabase
    from spark.db.engine import Executor

logger = logging.getLogger(__name__)

VALID_TYPES = {"system", "user", "assistant", "tool_call", "tool_result"}
_TOOL_RESULTS_MARKER = "[TOOL_RESULTS]"
_REBUILD_BATCH = 64


async def _add_element(
    db: Executor,
    /,
    store: VectorStore,
    conversation_id: int,
    element_type: str,
    content: str,
    embedding: Vector,
    user_guid: str,
    metadata_json: str | None,
) -> int | None:
    """Insert the element and record its vector in one transaction (a refused vector undoes both)."""
    from spark.database import context_index

    async with db.transaction() as conn:
        element_id = await context_index.add_index_element(
            conn,
            conversation_id,
            element_type,
            content,
            embedding,
            user_guid,
            metadata_json=metadata_json,
        )
        if element_id is not None:
            await store.add(conn, user_guid, element_id, embedding)
    return element_id


class ConversationVectorIndex:
    """Semantic search over conversation messages, tool calls, and results."""

    def __init__(
        self,
        db: SyncDatabase,
        conversation_id: int,
        user_guid: str,
        config: dict[str, Any] | None = None,
        *,
        embedder: EmbeddingModel | None = None,
        store: VectorStore | None = None,
    ) -> None:
        self._db = db
        self._conversation_id = conversation_id
        self._user_guid = user_guid
        config = config or {}
        emb_config = config.get("embedding", {})
        idx_config = config.get("indexing", {})

        self._embedder: EmbeddingModel = embedder or SentenceTransformerEmbedding(
            model_name=emb_config.get("model", "all-MiniLM-L6-v2"),
            device=emb_config.get("device", "cpu"),
        )
        self._store = store
        self._chunk_size = idx_config.get("chunk_size", 500)
        self._chunk_overlap = idx_config.get("chunk_overlap", 50)

        self._cache: list[dict[str, Any]] | None = None
        self._cache_dirty = True

    @property
    def vector_store(self) -> VectorStore:
        """The mode's store, chosen from the database's dialect on first use."""
        if self._store is None:
            self._store = vector_store(CONTEXT, self._db.database.dialect)
        return self._store

    def _embed(self, text: str) -> NDArray[np.float32]:
        row: NDArray[np.float32] = self._embedder.embed([text])[0]
        return row

    def index_element(
        self,
        element_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        """Index a content element. Returns ID or None if duplicate."""
        if element_type not in VALID_TYPES:
            return None
        content = content.strip()
        if not content:
            return None

        embedding = self._embed(content)
        metadata_json = json.dumps(metadata) if metadata else None

        result = self._db.call(
            _add_element,
            self.vector_store,
            self._conversation_id,
            element_type,
            content,
            embedding,
            self._user_guid,
            metadata_json,
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

        metadata: dict[str, Any] = {"message_id": message_id}
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
        query_embedding = self._embed(query)
        element_type = element_types[0] if element_types and len(element_types) == 1 else None
        return self._db.call(
            self.vector_store.search,
            self._user_guid,
            query_embedding,
            top_k=top_k,
            threshold=threshold,
            where={"conversation_id": [self._conversation_id], "element_type": element_type},
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
        query_embedding = self._embed(query)
        return self._db.call(
            self.vector_store.search,
            self._user_guid,
            query_embedding,
            top_k=top_k,
            threshold=threshold,
            where={"conversation_id": list(conversation_ids)},
        )

    def clear(self) -> None:
        """Clear all index elements for this conversation."""
        from spark.database import context_index

        self._db.call(context_index.delete_index_elements, self._conversation_id)
        self._cache_dirty = True
        self._cache = None

    def get_stats(self) -> dict[str, int]:
        """Get element counts by type."""
        from spark.database import context_index

        return self._db.call(context_index.get_index_stats, self._conversation_id)

    def rebuild(self) -> int:
        """Re-embed every context element of this user (all conversations); returns how many.

        Needed after changing the embedding model: until then a model of another
        dimension is refused with EmbeddingDimensionMismatch.
        """
        store = self.vector_store
        items = self._db.call(store.items, self._user_guid)
        vectors: list[tuple[int, Vector]] = []
        for start in range(0, len(items), _REBUILD_BATCH):
            batch = items[start : start + _REBUILD_BATCH]
            embedded = self._embedder.embed([text for _, text in batch])
            vectors.extend((item_id, embedded[i]) for i, (item_id, _) in enumerate(batch))
        result = self._db.call(store.rebuild, self._user_guid, vectors)
        self._cache_dirty = True
        return result

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
