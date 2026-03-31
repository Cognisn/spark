"""Lazy-loaded embedding model with thread-safe singleton caching."""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "all-MiniLM-L6-v2"


class EmbeddingModel:
    """Generates text embeddings using sentence-transformers.

    Uses lazy loading — the model is only downloaded/loaded on first use.
    Thread-safe singleton: multiple instances with the same model name share one loaded model.
    """

    _models: dict[str, Any] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
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
        if self._model_name in EmbeddingModel._models:
            return EmbeddingModel._models[self._model_name]

        with EmbeddingModel._lock:
            # Double-check inside lock
            if self._model_name in EmbeddingModel._models:
                return EmbeddingModel._models[self._model_name]

            logger.info("Loading embedding model: %s", self._model_name)
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self._model_name, device=self._device)
            EmbeddingModel._models[self._model_name] = model
            return model

    @property
    def model(self) -> Any:
        if self._model is None:
            self._model = self._load()
        return self._model

    @property
    def is_loaded(self) -> bool:
        return self._model_name in EmbeddingModel._models

    @property
    def embedding_dimension(self) -> int:
        return self.model.get_sentence_embedding_dimension()

    def encode(
        self,
        texts: str | list[str],
        *,
        batch_size: int | None = None,
        normalize: bool = True,
    ) -> np.ndarray:
        """Encode text(s) into embedding vectors.

        Returns shape (dim,) for a single string, (n, dim) for a list.
        """
        single = isinstance(texts, str)
        if single:
            texts = [texts]

        # Handle empty inputs
        if not texts or all(not t.strip() for t in texts):
            dim = self.embedding_dimension
            zeros = np.zeros((len(texts), dim), dtype=np.float32)
            return zeros[0] if single else zeros

        embeddings = self.model.encode(
            texts,
            batch_size=batch_size or self._batch_size,
            normalize_embeddings=normalize,
            show_progress_bar=False,
        )

        result = np.array(embeddings, dtype=np.float32)
        return result[0] if single else result

    @staticmethod
    def compute_similarity(
        query_embedding: np.ndarray,
        corpus_embeddings: np.ndarray,
    ) -> np.ndarray:
        """Compute cosine similarities between a query and corpus embeddings.

        Assumes L2-normalised embeddings (dot product = cosine similarity).
        """
        if corpus_embeddings.ndim == 1:
            corpus_embeddings = corpus_embeddings.reshape(1, -1)
        return np.dot(corpus_embeddings, query_embedding.T).flatten()
