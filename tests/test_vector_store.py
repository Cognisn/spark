"""The VectorStore contract on both engines: SQLite (personal mode) and pgvector (server mode).

Every test runs through ``spark_db``, so once on SQLite and once (marked
``postgres``) on PostgreSQL with pgvector. Vectors are small synthetic unit
vectors; nothing here loads a real embedding model.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from spark.database import context_index, conversations, memories
from spark.db import Database
from spark.index.vectors import (
    CONTEXT,
    MEMORIES,
    EmbeddingDimensionMismatch,
    PgVectorStore,
    SqliteVectorStore,
    VectorStore,
    vector_store,
)
from spark.mode import Dialect

ALICE = "user-alice-example"
BOB = "user-bob-example"
DIM = 8


def unit(*values: float, dim: int = DIM) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    vec[: len(values)] = values
    return vec / np.linalg.norm(vec)


async def add_memory(
    db: Database, user: str, content: str, vec: np.ndarray, category: str = "facts"
) -> int:
    store = vector_store(MEMORIES, db.dialect)
    async with db.transaction() as conn:
        mid = await memories.add_memory(conn, user, content, category, vec)
        assert mid is not None
        await store.add(conn, user, mid, vec)
    return mid


async def add_element(
    db: Database, user: str, conversation_id: int, text: str, vec: np.ndarray, kind: str = "user"
) -> int:
    store = vector_store(CONTEXT, db.dialect)
    async with db.transaction() as conn:
        eid = await context_index.add_index_element(conn, conversation_id, kind, text, vec, user)
        assert eid is not None
        await store.add(conn, user, eid, vec)
    return eid


# -- Selection and protocol ---------------------------------------------------------


def test_factory_picks_the_engine_store() -> None:
    assert isinstance(vector_store(MEMORIES, Dialect.SQLITE), SqliteVectorStore)
    assert isinstance(vector_store(MEMORIES, Dialect.POSTGRESQL), PgVectorStore)
    for store in (vector_store(CONTEXT, Dialect.SQLITE), vector_store(CONTEXT, Dialect.POSTGRESQL)):
        assert isinstance(store, VectorStore)
        assert store.collection is CONTEXT


def test_unknown_filter_is_refused() -> None:
    store = vector_store(MEMORIES, Dialect.POSTGRESQL)
    with pytest.raises(ValueError, match="element_type"):
        store.check_filters({"element_type": "user"})


# -- Memories -------------------------------------------------------------------------


async def test_search_orders_by_similarity_and_honours_top_k(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    near = await add_memory(spark_db, ALICE, "near", unit(1, 0.1))
    mid = await add_memory(spark_db, ALICE, "middle", unit(1, 1))
    await add_memory(spark_db, ALICE, "far", unit(0, 1))

    hits = await store.search(spark_db, ALICE, unit(1), top_k=2, threshold=0.0)

    assert [h["id"] for h in hits] == [near, mid]
    assert hits[0]["similarity"] == pytest.approx(float(unit(1) @ unit(1, 0.1)), abs=1e-5)
    assert hits[0]["content"] == "near"


async def test_search_applies_the_threshold(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    await add_memory(spark_db, ALICE, "close", unit(1, 0.2))
    await add_memory(spark_db, ALICE, "orthogonal", unit(0, 1))

    hits = await store.search(spark_db, ALICE, unit(1), top_k=10, threshold=0.5)

    assert [h["content"] for h in hits] == ["close"]


async def test_search_filters_by_category(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    await add_memory(spark_db, ALICE, "a fact", unit(1), category="facts")
    pref = await add_memory(spark_db, ALICE, "a preference", unit(1, 0.1), category="preferences")

    hits = await store.search(
        spark_db, ALICE, unit(1), top_k=10, threshold=0.0, where={"category": "preferences"}
    )

    assert [h["id"] for h in hits] == [pref]


async def test_another_users_memories_are_never_returned(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    # Bob's memories match the query exactly; Alice's only loosely.
    for i in range(5):
        await add_memory(spark_db, BOB, f"bob secret {i}", unit(1))
    own = await add_memory(spark_db, ALICE, "alice note", unit(1, 1))

    hits = await store.search(spark_db, ALICE, unit(1), top_k=10, threshold=-1.0)

    assert [h["id"] for h in hits] == [own]
    assert all(h["user_guid"] == ALICE for h in hits)


async def test_delete_removes_vectors_from_search(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    gone = await add_memory(spark_db, ALICE, "gone", unit(1))
    kept = await add_memory(spark_db, ALICE, "kept", unit(1, 0.5))

    removed = await store.delete(spark_db, ALICE, [gone])
    hits = await store.search(spark_db, ALICE, unit(1), top_k=10, threshold=0.0)

    assert removed == 1
    assert [h["id"] for h in hits] == [kept]


async def test_delete_never_touches_another_users_vectors(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    bobs = await add_memory(spark_db, BOB, "bob", unit(1))

    removed = await store.delete(spark_db, ALICE, [bobs])

    assert removed == 0
    assert [h["id"] for h in await store.search(spark_db, BOB, unit(1), threshold=0.0)] == [bobs]


async def test_add_refuses_an_item_of_another_user(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    bobs = await add_memory(spark_db, BOB, "bob", unit(1))

    with pytest.raises(LookupError):
        await store.add(spark_db, ALICE, bobs, unit(0, 1))


async def test_items_lists_the_users_texts(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    a = await add_memory(spark_db, ALICE, "first", unit(1))
    b = await add_memory(spark_db, ALICE, "second", unit(0, 1))
    await add_memory(spark_db, BOB, "not alice", unit(1))

    assert await store.items(spark_db, ALICE) == [(a, "first"), (b, "second")]


async def test_rebuild_replaces_every_vector_of_the_user(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    a = await add_memory(spark_db, ALICE, "a", unit(1, 0, dim=4))
    b = await add_memory(spark_db, ALICE, "b", unit(0, 1, dim=4))
    bobs = await add_memory(spark_db, BOB, "bob", unit(1, 0, dim=4))

    # A new model with another dimension: rebuild is how the index moves to it.
    count = await store.rebuild(spark_db, ALICE, [(a, unit(0, 1, dim=6)), (b, unit(1, 0, dim=6))])
    hits = await store.search(spark_db, ALICE, unit(1, 0, dim=6), top_k=1, threshold=0.0)

    assert count == 2
    assert [h["id"] for h in hits] == [b]
    # Bob's index is untouched.
    bob_hits = await store.search(spark_db, BOB, unit(1, 0, dim=4), threshold=0.0)
    assert [h["id"] for h in bob_hits] == [bobs]


async def test_rebuild_refuses_an_item_of_another_user(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    bobs = await add_memory(spark_db, BOB, "bob", unit(1))

    with pytest.raises(LookupError):
        await store.rebuild(spark_db, ALICE, [(bobs, unit(0, 1))])


# -- Dimension recorded and a mismatch refused ---------------------------------------


async def test_search_refuses_a_query_of_another_dimension(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    await add_memory(spark_db, ALICE, "eight", unit(1))

    with pytest.raises(EmbeddingDimensionMismatch) as caught:
        await store.search(spark_db, ALICE, unit(1, dim=16), threshold=0.0)

    message = str(caught.value)
    assert "8" in message and "16" in message and "rebuild" in message


async def test_add_refuses_a_vector_of_another_dimension(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    await add_memory(spark_db, ALICE, "eight", unit(1))

    with pytest.raises(EmbeddingDimensionMismatch):
        await add_memory(spark_db, ALICE, "sixteen", unit(1, dim=16))

    # The refused memory was rolled back with its vector.
    assert [m["content"] for m in await memories.get_all_memories(spark_db, ALICE)] == ["eight"]


async def test_dimensions_are_per_user(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)
    await add_memory(spark_db, BOB, "bob eight", unit(1))

    sixteen = await add_memory(spark_db, ALICE, "alice sixteen", unit(1, dim=16))
    hits = await store.search(spark_db, ALICE, unit(1, dim=16), threshold=0.0)

    assert [h["id"] for h in hits] == [sixteen]


async def test_empty_index_accepts_any_dimension(spark_db: Database) -> None:
    store = vector_store(MEMORIES, spark_db.dialect)

    assert await store.search(spark_db, ALICE, unit(1, dim=16), threshold=0.0) == []


# -- Conversation context ---------------------------------------------------------------


async def test_context_search_is_limited_to_the_conversations(spark_db: Database) -> None:
    store = vector_store(CONTEXT, spark_db.dialect)
    c1 = await conversations.create_conversation(spark_db, "one", "model", ALICE)
    c2 = await conversations.create_conversation(spark_db, "two", "model", ALICE)
    c3 = await conversations.create_conversation(spark_db, "three", "model", ALICE)
    e1 = await add_element(spark_db, ALICE, c1, "in one", unit(1, 0.1))
    e2 = await add_element(spark_db, ALICE, c2, "in two", unit(1, 0.2))
    await add_element(spark_db, ALICE, c3, "in three", unit(1))

    hits = await store.search(
        spark_db, ALICE, unit(1), top_k=10, threshold=0.0, where={"conversation_id": [c1, c2]}
    )

    assert [h["id"] for h in hits] == [e1, e2]
    assert "embedding" not in hits[0]


async def test_context_search_filters_by_element_type(spark_db: Database) -> None:
    store = vector_store(CONTEXT, spark_db.dialect)
    c1 = await conversations.create_conversation(spark_db, "one", "model", ALICE)
    await add_element(spark_db, ALICE, c1, "said", unit(1), kind="user")
    tool = await add_element(spark_db, ALICE, c1, "Tool: x", unit(1, 0.3), kind="tool_call")

    hits = await store.search(
        spark_db,
        ALICE,
        unit(1),
        threshold=0.0,
        where={"conversation_id": [c1], "element_type": "tool_call"},
    )

    assert [h["id"] for h in hits] == [tool]


async def test_context_search_without_conversations_is_empty(spark_db: Database) -> None:
    store = vector_store(CONTEXT, spark_db.dialect)

    assert await store.search(spark_db, ALICE, unit(1), where={"conversation_id": []}) == []


async def test_context_items_and_rebuild(spark_db: Database) -> None:
    store = vector_store(CONTEXT, spark_db.dialect)
    c1 = await conversations.create_conversation(spark_db, "one", "model", ALICE)
    e1 = await add_element(spark_db, ALICE, c1, "first", unit(1))
    e2 = await add_element(spark_db, ALICE, c1, "second", unit(0, 1))

    assert await store.items(spark_db, ALICE) == [(e1, "first"), (e2, "second")]
    assert await store.rebuild(spark_db, ALICE, [(e1, unit(0, 1)), (e2, unit(1))]) == 2
    hits = await store.search(
        spark_db, ALICE, unit(1), top_k=1, threshold=0.0, where={"conversation_id": [c1]}
    )
    assert [h["id"] for h in hits] == [e2]


# -- PostgreSQL only: the in-database search ------------------------------------------


@pytest.mark.postgres
async def test_context_search_never_returns_another_users_elements(spark_db: Database) -> None:
    if spark_db.dialect is not Dialect.POSTGRESQL:
        pytest.skip("server mode")
    store = vector_store(CONTEXT, spark_db.dialect)
    own = await conversations.create_conversation(spark_db, "mine", "model", ALICE)
    theirs = await conversations.create_conversation(spark_db, "theirs", "model", BOB)
    mine = await add_element(spark_db, ALICE, own, "mine", unit(1, 1))
    await add_element(spark_db, BOB, theirs, "theirs", unit(1))

    # Even when asked for Bob's conversation, Alice only ever sees her own elements.
    hits = await store.search(
        spark_db, ALICE, unit(1), threshold=-1.0, where={"conversation_id": [own, theirs]}
    )

    assert [h["id"] for h in hits] == [mine]


def _pg_only(db: Database) -> None:
    if db.dialect is not Dialect.POSTGRESQL:
        pytest.skip("server mode")


@pytest.mark.postgres
async def test_vectors_follow_their_memory_when_it_is_deleted(spark_db: Database) -> None:
    _pg_only(spark_db)
    mid = await add_memory(spark_db, ALICE, "x", unit(1))

    await memories.delete_memory(spark_db, mid, ALICE)

    assert await spark_db.fetch_value("SELECT COUNT(*) FROM memory_vectors") == 0


@pytest.mark.postgres
async def test_stored_vector_records_its_dimension(spark_db: Database) -> None:
    _pg_only(spark_db)
    mid = await add_memory(spark_db, ALICE, "x", unit(1))

    row = await spark_db.fetch_one(
        "SELECT dimension, vector_dims(embedding) AS dims FROM memory_vectors WHERE memory_id = ?",
        (mid,),
    )

    assert row == {"dimension": DIM, "dims": DIM}


@pytest.mark.postgres
@pytest.mark.parametrize("table", ["memory_vectors", "context_vectors"])
async def test_hnsw_cosine_index_serves_the_search(spark_db: Database, table: str) -> None:
    _pg_only(spark_db)
    index = await spark_db.fetch_one(
        "SELECT indexdef FROM pg_indexes WHERE tablename = ? AND indexname = ?",
        (table, f"{table}_hnsw_384"),
    )
    assert index is not None
    assert "hnsw" in index["indexdef"] and "vector_cosine_ops" in index["indexdef"]

    store = vector_store(MEMORIES if table == "memory_vectors" else CONTEXT, spark_db.dialect)
    assert isinstance(store, PgVectorStore)
    where = {} if table == "memory_vectors" else {"conversation_id": [1]}
    async with spark_db.transaction() as conn:
        # A selective user filter makes an exact scan of the user's rows cheaper; take
        # that plan away to show the HNSW index can serve this query shape.
        await conn.execute("SET LOCAL enable_seqscan = off")
        await conn.execute("SET LOCAL enable_sort = off")
        plan = await store.explain(conn, ALICE, unit(1, dim=384), top_k=5, where=where)
    assert f"{table}_hnsw_384" in plan


@pytest.mark.postgres
async def test_filtered_search_finds_the_users_rows_among_many_others(spark_db: Database) -> None:
    """HNSW visits a bounded candidate list; the user filter must not starve it."""
    _pg_only(spark_db)
    rng = np.random.default_rng(7)
    target = unit(1, dim=384)
    noise = rng.normal(size=(1500, 384)).astype(np.float32) * 0.05
    others = noise + target
    others /= np.linalg.norm(others, axis=1, keepdims=True)
    async with spark_db.transaction() as conn:
        ids = []
        for i, vec in enumerate(others):
            mid = await memories.add_memory(conn, BOB, f"bob {i}", "facts", vec)
            ids.append((mid, vec))
        await conn.execute_many(
            "INSERT INTO memory_vectors (memory_id, dimension, embedding) VALUES (?, 384, ?)",
            [(mid, vec) for mid, vec in ids],
        )
    own = [
        await add_memory(spark_db, ALICE, f"alice {i}", unit(1, 0.5 + i, dim=384)) for i in range(5)
    ]
    await spark_db.execute("ANALYZE memory_vectors")

    store = vector_store(MEMORIES, spark_db.dialect)
    assert isinstance(store, PgVectorStore)
    async with spark_db.transaction() as conn:
        # Force the approximate HNSW path (the planner would scan Alice's few rows exactly).
        await conn.execute("SET LOCAL enable_seqscan = off")
        await conn.execute("SET LOCAL enable_sort = off")
        # Search first: explain sets the same transaction-local scan settings.
        hits = await store.search(conn, ALICE, target, top_k=5, threshold=-1.0)
        assert "memory_vectors_hnsw_384" in await store.explain(conn, ALICE, target, top_k=5)

    assert sorted(h["id"] for h in hits) == sorted(own)


@pytest.mark.postgres
async def test_vector_table_write_is_scoped_to_the_user(spark_db: Database) -> None:
    """The pgvector write checks ownership itself, not only through the column write."""
    _pg_only(spark_db)
    store = vector_store(MEMORIES, spark_db.dialect)
    assert isinstance(store, PgVectorStore)
    bobs = await add_memory(spark_db, BOB, "bob", unit(1))

    with pytest.raises(LookupError):
        await store._write_vector(spark_db, ALICE, bobs, unit(0, 1))

    assert [h["id"] for h in await store.search(spark_db, BOB, unit(1), threshold=0.9)] == [bobs]


@pytest.mark.postgres
async def test_pool_connections_adapt_numpy_vectors(spark_db: Database) -> None:
    _pg_only(spark_db)
    value: Any = await spark_db.fetch_value("SELECT ?::vector", (np.array([1, 2, 3], np.float32),))

    assert isinstance(value, np.ndarray)
    assert value.tolist() == [1.0, 2.0, 3.0]
