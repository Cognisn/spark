"""Vector embedding, memory, and context indexing."""

from spark.index.embeddings import SentenceTransformerEmbedding
from spark.index.memory_index import MemoryIndex
from spark.index.vector_index import ConversationVectorIndex
from spark.index.vectors import (
    CONTEXT,
    MEMORIES,
    Collection,
    EmbeddingDimensionMismatch,
    EmbeddingModel,
    VectorStore,
    vector_store,
)

__all__ = [
    "CONTEXT",
    "MEMORIES",
    "Collection",
    "ConversationVectorIndex",
    "EmbeddingDimensionMismatch",
    "EmbeddingModel",
    "MemoryIndex",
    "SentenceTransformerEmbedding",
    "VectorStore",
    "vector_store",
]
