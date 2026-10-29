"""Unique natural keys on kg_nodes, kg_edges, skills and conversation_skills (revision vector_store_2).

* Parallel first inserts: many tasks on separate pool connections upsert the same
  key at once; exactly one row results, carrying every merge. Before the unique
  constraints, PostgreSQL (READ COMMITTED) let concurrent select-then-insert
  transactions each insert their own row.
* The migration: a database at vector_store_1 holding duplicates is merged
  (earliest row kept, references re-pointed), constrained, and the downgrade
  restores the unconstrained schema.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from spark.database import conversations, skills
from spark.db import Database, migrate
from spark.knowledge import store
from spark.mode import DatabaseSettings, Dialect

USER = "user-dana-example"
OTHER = "user-eve-example"
PARALLEL = 8
KEYS = 25
REVISION = "vector_store_2"
BEFORE = "vector_store_1"


async def _all_at_once(n: int, work: Callable[[], Awaitable[Any]]) -> list[Any]:
    """Run ``work`` n times concurrently, released together by a barrier."""
    barrier = asyncio.Barrier(n)

    async def one() -> Any:
        await barrier.wait()
        return await work()

    return list(await asyncio.gather(*(one() for _ in range(n))))


# -- Parallel first inserts ------------------------------------------------------------


@pytest.fixture
async def wide_db(spark_db_settings: DatabaseSettings) -> AsyncIterator[Database]:
    """A database whose pool already holds one connection per parallel task."""
    db = await Database.open(spark_db_settings, min_size=PARALLEL, max_size=PARALLEL)
    try:
        yield db
    finally:
        await db.close()


async def test_parallel_first_node_inserts_make_one_node(wide_db: Database) -> None:
    for k in range(KEYS):
        ids = await _all_at_once(
            PARALLEL,
            lambda: store.upsert_node(wide_db, "global", f"Example Org {k}", "org", "", None, USER),
        )
        assert len(set(ids)) == 1, k

    rows = await wide_db.fetch_all(
        "SELECT name_key, COUNT(*) AS n, SUM(weight) AS w FROM kg_nodes WHERE user_guid = ? "
        "GROUP BY name_key",
        (USER,),
    )
    assert len(rows) == KEYS
    assert {(int(r["n"]), int(r["w"])) for r in rows} == {(1, PARALLEL)}


async def test_parallel_first_edge_inserts_make_one_edge(wide_db: Database) -> None:
    a = await store.upsert_node(wide_db, "global", "Alpha", "org", "", None, USER)
    b = await store.upsert_node(wide_db, "global", "Beta", "org", "", None, USER)

    for k in range(KEYS):
        ids = await _all_at_once(
            PARALLEL, lambda: store.upsert_edge(wide_db, "global", a, b, f"Relation {k}", "", USER)
        )
        assert len(set(ids)) == 1, k

    rows = await wide_db.fetch_all(
        "SELECT relation, COUNT(*) AS n, SUM(weight) AS w FROM kg_edges WHERE user_guid = ? "
        "GROUP BY relation",
        (USER,),
    )
    assert len(rows) == KEYS
    assert {(int(r["n"]), int(r["w"])) for r in rows} == {(1, PARALLEL)}


async def test_parallel_first_skill_states_make_one_row(wide_db: Database) -> None:
    for k in range(KEYS):
        await _all_at_once(
            PARALLEL, lambda: skills.set_skill_enabled(wide_db, f"example-skill-{k}", False, USER)
        )

    counts = await wide_db.fetch_all(
        "SELECT name, COUNT(*) AS n FROM skills WHERE user_guid = ? GROUP BY name", (USER,)
    )
    assert len(counts) == KEYS
    assert {int(r["n"]) for r in counts} == {1}
    assert set((await skills.get_skill_states(wide_db, USER)).values()) == {False}


async def test_parallel_first_conversation_skill_states_make_one_row(wide_db: Database) -> None:
    cid = await conversations.create_conversation(wide_db, "c", "model", USER)

    for k in range(KEYS):
        await _all_at_once(
            PARALLEL,
            lambda: skills.set_conversation_skill_enabled(
                wide_db, cid, f"example-skill-{k}", False
            ),
        )

    counts = await wide_db.fetch_all(
        "SELECT skill_name, COUNT(*) AS n FROM conversation_skills WHERE conversation_id = ? "
        "GROUP BY skill_name",
        (cid,),
    )
    assert len(counts) == KEYS
    assert {int(r["n"]) for r in counts} == {1}


# -- Upsert semantics on the constraints -------------------------------------------------


async def test_node_merge_keeps_description_and_embedding_unless_given(spark_db: Database) -> None:
    first = await store.upsert_node(spark_db, "global", "Gamma", "org", "a firm", b"v1", USER)
    again = await store.upsert_node(spark_db, "global", " gamma ", "person", "", None, USER)
    newer = await store.upsert_node(spark_db, "global", "GAMMA", "org", "the firm", b"v2", USER)

    row = await spark_db.fetch_one(
        "SELECT name, entity_type, description, embedding, weight FROM kg_nodes WHERE id = ?",
        (first,),
    )
    assert first == again == newer
    assert row == {
        "name": "Gamma",
        "entity_type": "org",
        "description": "the firm",
        "embedding": b"v2",
        "weight": 3,
    }


async def test_edge_relation_matches_without_case_or_spaces(spark_db: Database) -> None:
    a = await store.upsert_node(spark_db, "global", "Alpha", "org", "", None, USER)
    b = await store.upsert_node(spark_db, "global", "Beta", "org", "", None, USER)

    first = await store.upsert_edge(spark_db, "global", a, b, "Works With", "d", USER)
    second = await store.upsert_edge(spark_db, "global", a, b, " works with ", "", USER)
    other = await store.upsert_edge(spark_db, "global", a, b, "owns", "", USER)

    assert first == second != other
    edge = await spark_db.fetch_one("SELECT relation, weight FROM kg_edges WHERE id = ?", (first,))
    assert edge == {"relation": "Works With", "weight": 2}


async def test_same_key_for_another_user_is_another_node(spark_db: Database) -> None:
    mine = await store.upsert_node(spark_db, "global", "Delta", "org", "", None, USER)
    theirs = await store.upsert_node(spark_db, "global", "Delta", "org", "", None, OTHER)

    assert mine != theirs


# -- The migration ------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def empty_settings(request: pytest.FixtureRequest, tmp_path: Path) -> DatabaseSettings:
    if request.param == "sqlite":
        return DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    return DatabaseSettings(Dialect.POSTGRESQL, url=request.getfixturevalue("pg_url"))


class _Raw:
    """Plain driver access, independent of the code under test."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings

    def run(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if self.settings.dialect is Dialect.SQLITE:
            assert self.settings.sqlite_path is not None
            with sqlite3.connect(self.settings.sqlite_path) as conn:
                cur = conn.execute(sql, params)
                return cur.fetchall() if cur.description else []
        assert self.settings.url is not None
        with psycopg.connect(self.settings.url, options="-c search_path=spark,public") as conn:
            cur = conn.execute(sql.replace("?", "%s"), params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []

    def insert(self, sql: str, params: tuple[Any, ...]) -> int:
        rows = self.run(sql + " RETURNING id", params)
        return int(rows[0][0])

    def columns(self, table: str) -> list[str]:
        if self.settings.dialect is Dialect.SQLITE:
            return [r[1] for r in self.run(f"PRAGMA table_info({table})")]
        rows = self.run(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'spark' AND table_name = ? ORDER BY ordinal_position",
            (table,),
        )
        return [r[0] for r in rows]

    def unique_indexes(self, table: str) -> set[str]:
        if self.settings.dialect is Dialect.SQLITE:
            return {r[1] for r in self.run(f"PRAGMA index_list({table})") if r[2] and r[3] == "c"}
        rows = self.run(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'spark' AND tablename = ? "
            "AND indexdef LIKE 'CREATE UNIQUE INDEX%%' AND indexname NOT LIKE '%%_pkey'",
            (table,),
        )
        return {r[0] for r in rows}


TABLES = ("kg_nodes", "kg_edges", "skills", "conversation_skills")


def _seed_duplicates(raw: _Raw) -> dict[str, int]:
    """Duplicates as concurrent 0.2.x or 0.3.0a1 inserts could leave them."""
    ids: dict[str, int] = {}
    ids["conv"] = raw.insert("INSERT INTO conversations (name, user_guid) VALUES ('c', ?)", (USER,))
    node = (
        "INSERT INTO kg_nodes (scope, name, name_key, entity_type, description, embedding, "
        "weight, user_guid, updated_at) VALUES ('global', ?, ?, 'org', ?, ?, ?, ?, ?)"
    )
    ids["n1"] = raw.insert(node, ("Acme", "acme", "", None, 2, USER, "2026-01-01 00:00:00"))
    ids["n2"] = raw.insert(
        node, ("ACME", "acme", "a company", b"e2", 1, USER, "2026-01-03 00:00:00")
    )
    ids["n3"] = raw.insert(
        node, ("acme", "acme", "an example company", None, 3, USER, "2026-01-02 00:00:00")
    )
    ids["b1"] = raw.insert(node, ("Beta", "beta", "", None, 1, USER, "2026-01-01 00:00:00"))
    ids["o1"] = raw.insert(node, ("Acme", "acme", "", None, 1, OTHER, "2026-01-01 00:00:00"))
    ids["o2"] = raw.insert(
        node, ("Acme", "acme", "", None, 1, "user-frank-example", "2026-01-01 00:00:00")
    )
    edge = (
        "INSERT INTO kg_edges (scope, source_node_id, target_node_id, relation, description, "
        "weight, user_guid) VALUES ('global', ?, ?, ?, ?, ?, ?)"
    )
    ids["e1"] = raw.insert(edge, (ids["n1"], ids["b1"], "works with", "", 1, USER))
    ids["e2"] = raw.insert(edge, (ids["n2"], ids["b1"], "Works With ", "", 2, USER))
    ids["e3"] = raw.insert(edge, (ids["b1"], ids["n3"], "owns", "", 1, USER))
    skill = "INSERT INTO skills (name, enabled, user_guid) VALUES (?, ?, ?)"
    ids["s1"] = raw.insert(skill, ("pdf", 1, USER))
    ids["s2"] = raw.insert(skill, ("pdf", 0, USER))
    ids["s3"] = raw.insert(skill, ("pdf", 1, OTHER))
    conv_skill = (
        "INSERT INTO conversation_skills (conversation_id, skill_name, enabled) VALUES (?, ?, ?)"
    )
    ids["cs1"] = raw.insert(conv_skill, (ids["conv"], "pdf", 0))
    ids["cs2"] = raw.insert(conv_skill, (ids["conv"], "pdf", 1))
    return ids


def test_migration_merges_duplicates_and_constrains(empty_settings: DatabaseSettings) -> None:
    migrate.upgrade(empty_settings, target=BEFORE)
    raw = _Raw(empty_settings)
    ids = _seed_duplicates(raw)

    migrate.upgrade(empty_settings, target=REVISION)

    nodes = raw.run(
        "SELECT id, name, description, embedding, weight, updated_at, user_guid FROM kg_nodes "
        "ORDER BY id"
    )
    n1, b1, o1, o2 = ids["n1"], ids["b1"], ids["o1"], ids["o2"]
    assert [(r[0], r[1], r[2], bytes(r[3]) if r[3] else None, r[4]) for r in nodes] == [
        # Earliest row kept, weights summed, the latest non-empty description and embedding.
        (n1, "Acme", "an example company", b"e2", 6),
        (b1, "Beta", "", None, 1),
        # Other users' nodes with the same name are not merged.
        (o1, "Acme", "", None, 1),
        (o2, "Acme", "", None, 1),
    ]
    assert str(nodes[0][5]).startswith("2026-01-03")
    edges = raw.run(
        "SELECT id, source_node_id, target_node_id, relation, relation_key, weight FROM kg_edges "
        "ORDER BY id"
    )
    # e2 re-pointed to n1 duplicates e1 and merges into it; e3 re-pointed to n1.
    assert edges == [
        (ids["e1"], n1, b1, "works with", "works with", 3),
        (ids["e3"], b1, n1, "owns", "owns", 1),
    ]
    assert raw.run("SELECT id, name, enabled, user_guid FROM skills ORDER BY id") == [
        (ids["s1"], "pdf", 0, USER),  # earliest row, latest state
        (ids["s3"], "pdf", 1, OTHER),
    ]
    assert raw.run("SELECT id, enabled FROM conversation_skills ORDER BY id") == [(ids["cs1"], 1)]
    for table in TABLES:
        assert raw.unique_indexes(table), table


def test_constraints_refuse_a_duplicate_row(empty_settings: DatabaseSettings) -> None:
    migrate.upgrade(empty_settings, target=REVISION)
    raw = _Raw(empty_settings)
    raw.run("INSERT INTO skills (name, enabled, user_guid) VALUES ('pdf', 1, ?)", (USER,))

    with pytest.raises((sqlite3.IntegrityError, psycopg.errors.UniqueViolation)):
        raw.run("INSERT INTO skills (name, enabled, user_guid) VALUES ('pdf', 0, ?)", (USER,))


def test_downgrade_restores_the_unconstrained_schema(
    empty_settings: DatabaseSettings, tmp_path: Path
) -> None:
    migrate.upgrade(empty_settings, target=BEFORE)
    raw = _Raw(empty_settings)
    before = {t: (raw.columns(t), raw.unique_indexes(t)) for t in TABLES}
    _seed_duplicates(raw)
    migrate.upgrade(empty_settings, target=REVISION)

    migrate.downgrade(empty_settings, target=BEFORE)

    assert {t: (raw.columns(t), raw.unique_indexes(t)) for t in TABLES} == before
    assert migrate.current(empty_settings) == BEFORE
    # Duplicates can be written again, as before the revision.
    raw.run("INSERT INTO skills (name, enabled, user_guid) VALUES ('pdf', 1, ?)", (USER,))
    migrate.upgrade(empty_settings, target=REVISION)
    assert raw.run("SELECT COUNT(*) FROM skills WHERE user_guid = ?", (USER,)) == [(1,)]
