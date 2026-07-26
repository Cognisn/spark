"""Knowledge graph query tests."""

from __future__ import annotations

import numpy as np
import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.knowledge import store
from spark.knowledge.query import find_entities, render_subgraph, subgraph_for_query


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)
    return conn


class DirectionalEmbedder:
    """Maps known words to distinct unit vectors so similarity is controllable."""

    _table = {"spark": [1, 0, 0], "coffee": [0, 1, 0]}

    def encode(self, text, normalize=True):
        for word, vec in self._table.items():
            if word in text.lower():
                return np.array(vec, dtype=np.float32)
        return np.array([0, 0, 1], dtype=np.float32)


def _seed_graph(db) -> tuple[int, int]:
    emb = DirectionalEmbedder()
    a = store.upsert_node(
        db, "global", "Spark", "project", "AI kit", emb.encode("spark").tobytes(), "u1"
    )
    b = store.upsert_node(
        db, "global", "Cognisn", "organisation", "the org", emb.encode("cognisn").tobytes(), "u1"
    )
    store.upsert_edge(db, "global", a, b, "belongs_to", "", "u1")
    return a, b


class TestFindAndExpand:
    def test_similarity_match_and_hop(self, db) -> None:
        _seed_graph(db)
        emb = DirectionalEmbedder()
        found = find_entities(db, emb, ["global"], "tell me about spark", "u1")
        assert found and found[0]["name"] == "Spark"

        text = subgraph_for_query(db, emb, ["global"], "spark", "u1")
        assert "Spark" in text and "Cognisn" in text and "belongs_to" in text

    def test_no_match_gives_empty(self, db) -> None:
        _seed_graph(db)
        assert subgraph_for_query(db, DirectionalEmbedder(), ["global"], "coffee", "u1") == ""


def test_render_truncates() -> None:
    nodes = [
        {
            "id": i,
            "name": f"N{i}",
            "entity_type": "concept",
            "description": "x" * 200,
            "weight": 1,
            "scope": "global",
        }
        for i in range(50)
    ]
    out = render_subgraph(nodes, [], max_chars=500)
    assert len(out) <= 560 and "truncated" in out.lower()
