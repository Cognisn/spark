"""The EmbeddingModel protocol and its local sentence-transformers implementation.

A stand-in replaces ``SentenceTransformer`` so nothing is downloaded; the
implementation's own work (batching, normalisation, CPU device, shapes) is real.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

import spark.index
from spark.index import embeddings
from spark.index.embeddings import SentenceTransformerEmbedding
from spark.index.vectors import EmbeddingModel


class FakeSentenceTransformer:
    """Records how it was built and called; returns deterministic unnormalised vectors."""

    instances: list[FakeSentenceTransformer] = []

    def __init__(self, name: str, device: str) -> None:
        self.name = name
        self.device = device
        self.calls: list[dict[str, Any]] = []
        FakeSentenceTransformer.instances.append(self)

    def get_sentence_embedding_dimension(self) -> int:
        return 4

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append({"texts": list(texts), **kwargs})
        out = np.array([[len(t), 1.0, 0.0, 0.0] for t in texts], dtype=np.float64)
        if kwargs.get("normalize_embeddings"):
            out /= np.linalg.norm(out, axis=1, keepdims=True)
        return out


@pytest.fixture(autouse=True)
def fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentence_transformers

    FakeSentenceTransformer.instances.clear()
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", FakeSentenceTransformer)
    monkeypatch.setattr(SentenceTransformerEmbedding, "_models", {})


def test_local_model_satisfies_the_protocol() -> None:
    assert isinstance(SentenceTransformerEmbedding("fake-model"), EmbeddingModel)


def test_package_exports_the_protocol_and_keeps_the_old_name() -> None:
    assert spark.index.EmbeddingModel is EmbeddingModel
    assert spark.index.SentenceTransformerEmbedding is SentenceTransformerEmbedding
    # 0.2.0 callers construct spark.index.embeddings.EmbeddingModel(); it stays the local model.
    assert embeddings.EmbeddingModel is SentenceTransformerEmbedding


def test_name_and_dimension() -> None:
    model = SentenceTransformerEmbedding("fake-model")

    assert model.name == "fake-model"
    assert model.dimension == 4


def test_defaults_to_the_cpu() -> None:
    SentenceTransformerEmbedding("fake-model").embed(["x"])

    assert FakeSentenceTransformer.instances[0].device == "cpu"


def test_embed_returns_one_normalised_float32_row_per_text() -> None:
    model = SentenceTransformerEmbedding("fake-model", batch_size=7)

    out = model.embed(["a", "abc"])

    assert out.dtype == np.float32
    assert out.shape == (2, 4)
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)
    call = FakeSentenceTransformer.instances[0].calls[0]
    assert call["batch_size"] == 7 and call["normalize_embeddings"] is True


def test_embed_of_nothing_is_an_empty_matrix_of_the_right_width() -> None:
    out = SentenceTransformerEmbedding("fake-model").embed([])

    assert out.shape == (0, 4)
    assert out.dtype == np.float32


def test_blank_texts_embed_to_zero_rows_as_before() -> None:
    model = SentenceTransformerEmbedding("fake-model")

    out = model.embed(["", "  "])

    assert out.shape == (2, 4)
    assert not out.any()


def test_one_loaded_model_is_shared_per_name() -> None:
    SentenceTransformerEmbedding("fake-model").embed(["x"])
    SentenceTransformerEmbedding("fake-model").embed(["y"])
    SentenceTransformerEmbedding("other-model").embed(["z"])

    assert [m.name for m in FakeSentenceTransformer.instances] == ["fake-model", "other-model"]


def test_encode_keeps_its_0_2_0_shapes() -> None:
    model = SentenceTransformerEmbedding("fake-model")

    assert model.encode("one").shape == (4,)
    assert model.encode(["one", "two"]).shape == (2, 4)
    assert model.embedding_dimension == 4
