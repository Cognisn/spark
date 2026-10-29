"""The local embedding model: sentence-transformers on the CPU, loaded lazily and shared.

:class:`SentenceTransformerEmbedding` implements :class:`spark.index.vectors.EmbeddingModel`.
``EmbeddingModel`` in this module is its 0.2.0 name, kept so existing callers
construct it unchanged; new code types against the protocol in :mod:`spark.index`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

__all__ = ["DEFAULT_MODEL", "EmbeddingModel", "SentenceTransformerEmbedding"]

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "all-MiniLM-L6-v2"


class SentenceTransformerEmbedding:
    """Generates text embeddings using sentence-transformers.

    Uses lazy loading: the model is only downloaded and loaded on first use.
    Thread-safe singleton: instances with the same model name share one loaded model.
    """

    _models: dict[str, Any] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        device: str = "cpu",
        batch_size: int = 32,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._model: Any = None

    def _load(self) -> Any:
        """Load or retrieve the cached model (thread-safe)."""
        models = SentenceTransformerEmbedding._models
        if self._model_name in models:
            return models[self._model_name]

        with SentenceTransformerEmbedding._lock:
            # Double-check inside lock
            if self._model_name in models:
                return models[self._model_name]

            logger.info("Loading embedding model: %s", self._model_name)
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self._model_name, device=self._device)
            models[self._model_name] = model
            return model

    @property
    def model(self) -> Any:
        if self._model is None:
            self._model = self._load()
        return self._model

    @property
    def is_loaded(self) -> bool:
        return self._model_name in SentenceTransformerEmbedding._models

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return int(self.model.get_sentence_embedding_dimension())

    @property
    def embedding_dimension(self) -> int:
        return self.dimension

    def embed(self, texts: Sequence[str]) -> NDArray[np.float32]:
        """L2-normalised embeddings, one row per text: shape ``(len(texts), dimension)``."""
        return self._encode_batch(list(texts), batch_size=None, normalize=True)

    def encode(
        self,
        texts: str | list[str],
        *,
        batch_size: int | None = None,
        normalize: bool = True,
    ) -> NDArray[np.float32]:
        """Encode text(s) into embedding vectors.

        Returns shape (dim,) for a single string, (n, dim) for a list.
        """
        if isinstance(texts, str):
            batch = self._encode_batch([texts], batch_size=batch_size, normalize=normalize)
            row: NDArray[np.float32] = batch[0]
            return row
        return self._encode_batch(texts, batch_size=batch_size, normalize=normalize)

    def _encode_batch(
        self, texts: list[str], *, batch_size: int | None, normalize: bool
    ) -> NDArray[np.float32]:
        # Empty or blank inputs embed to zero rows without calling the model.
        if not texts or all(not t.strip() for t in texts):
            return np.zeros((len(texts), self.dimension), dtype=np.float32)

        embeddings = self.model.encode(
            texts,
            batch_size=batch_size or self._batch_size,
            normalize_embeddings=normalize,
            show_progress_bar=False,
        )
        return np.asarray(embeddings, dtype=np.float32)

    @staticmethod
    def compute_similarity(
        query_embedding: NDArray[Any],
        corpus_embeddings: NDArray[Any],
    ) -> NDArray[Any]:
        """Compute cosine similarities between a query and corpus embeddings.

        Assumes L2-normalised embeddings (dot product = cosine similarity).
        """
        if corpus_embeddings.ndim == 1:
            corpus_embeddings = corpus_embeddings.reshape(1, -1)
        similarities: NDArray[Any] = np.dot(corpus_embeddings, query_embedding.T).flatten()
        return similarities


# The 0.2.0 name (web/server.py and knowledge/tools.py construct it).
EmbeddingModel = SentenceTransformerEmbedding
