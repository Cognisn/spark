"""Importing a bundle for a server user (A15): what is imported, dropped, refused and reported.

Each test runs on SQLite and (marked postgres) PostgreSQL. The bundle is
exported from a seeded personal SQLite database (tests.bundle_seed).
"""

from __future__ import annotations

import io
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from spark.bundles import format as bf
from spark.bundles.importer import ImportRefused, ImportTarget, import_bundle
from spark.bundles.reader import Bundle, Limits, open_bundle
from spark.files.store import MountedVolumeStore
from tests.bundle_seed import Seeded, ins, personal_bundle, vector

TARGET = "server-user-guid"
SERVER_MCP = [{"name": "remote-search", "transport": "http"}]


@pytest.fixture
async def exported(_sqlite_template: Path, tmp_path: Path) -> tuple[bytes, Seeded]:
    data, seeded, _ = await personal_bundle(_sqlite_template, tmp_path)
    return data, seeded


@pytest.fixture
def store(tmp_path: Path) -> MountedVolumeStore:
    return MountedVolumeStore(tmp_path / "server-files")


def _target(store: Any, user: str = TARGET, **overrides: Any) -> ImportTarget:
    values: dict[str, Any] = {
        "user_guid": user,
        "store": store,
        "quota_bytes": None,
        "mcp_servers": SERVER_MCP,
    }
    values.update(overrides)
    return ImportTarget(**values)


def _open(data: bytes) -> Bundle:
    return open_bundle(io.BytesIO(data), Limits())


async def _import(
    db: Any,
    data: bytes,
    target: ImportTarget,
    within: Callable[[Any, Any], Awaitable[None]] | None = None,
) -> Any:
    with _open(data) as bundle:
        return await import_bundle(db, bundle, target, within=within)


async def _count(db: Any, table: str, user: str = TARGET) -> int:
    return int(await db.fetch_value(f"SELECT COUNT(*) FROM {table} WHERE user_guid = ?", (user,)))


# -- What arrives ----------------------------------------------------------------------


async def test_every_section_arrives_for_the_target_user(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    assert report.imported["conversations"] == 2
    assert report.imported["messages"] == 3
    assert report.imported["user_memories"] == 2
    assert report.imported["kg_nodes"] == 3
    assert report.imported["autonomous_actions"] == 1
    assert report.imported["debate_exhibits"] == 1
    assert await _count(spark_db, "conversations") == 2
    assert await _count(spark_db, "messages") == 3
    assert await _count(spark_db, "user_memories") == 2
    assert await _count(spark_db, "kg_edges") == 1
    # Nothing is attached to anyone else.
    assert await _count(spark_db, "conversations", "") == 0


async def test_foreign_keys_point_at_the_new_rows(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    conversations = report.ids["conversations"]
    rows = await spark_db.fetch_all(
        "SELECT conversation_id FROM messages WHERE user_guid = ?", (TARGET,)
    )
    assert {r["conversation_id"] for r in rows} == {next(iter(conversations.values()))}
    link = await spark_db.fetch_one(
        "SELECT source_conversation_id, target_conversation_id FROM conversation_links "
        "WHERE user_guid = ?",
        (TARGET,),
    )
    assert set(link.values()) == set(conversations.values())
    exhibit_turn = await spark_db.fetch_value("SELECT turn_id FROM debate_exhibits")
    assert exhibit_turn in report.ids["debate_turns"].values()
    edge = await spark_db.fetch_one(
        "SELECT source_node_id, target_node_id, scope FROM kg_edges WHERE user_guid = ?", (TARGET,)
    )
    assert {edge["source_node_id"], edge["target_node_id"]} <= set(report.ids["kg_nodes"].values())
    chat = report.ids["conversations"][min(report.ids["conversations"])]
    assert edge["scope"] == f"conv:{chat}"


async def test_knowledge_graph_watermarks_follow_the_new_ids(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, seeded = exported
    report = await _import(spark_db, data, _target(store))
    builds = {
        r["source_key"]: r["watermark"]
        for r in await spark_db.fetch_all(
            "SELECT source_key, watermark FROM kg_builds WHERE user_guid = ?", (TARGET,)
        )
    }
    chat = report.ids["conversations"][seeded.conversations[0]]
    assert builds == {
        f"conv:{chat}": report.ids["messages"][seeded.messages[1]],
        "memories": report.ids["user_memories"][seeded.memories[0]],
    }


async def test_scheduled_actions_arrive_disabled_and_unlocked(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    action = await spark_db.fetch_one(
        "SELECT name, is_enabled, locked_by, locked_at FROM autonomous_actions WHERE user_guid = ?",
        (TARGET,),
    )
    assert action == {
        "name": "Morning garden check",
        "is_enabled": 0,
        "locked_by": None,
        "locked_at": None,
    }
    assert report.actions_disabled == ["Morning garden check"]
    assert await _count(spark_db, "action_runs") == 1


async def test_what_a_server_cannot_run_is_dropped_and_listed(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    assert sorted(report.dropped) == sorted(
        [
            '"Always" approval of read_file (a local-only tool)',
            '"Always" approval of notes__search (an MCP tool)',
            "conversation approval of run_command (a local-only tool)",
            "conversation tool filesystem (a local-only tool)",
            "conversation MCP server local-notes (stdio, which a server does not run)",
            "conversation MCP server retired-server (not offered on this server)",
            "action Morning garden check: tool notes__search of MCP server local-notes "
            "(stdio, which a server does not run)",
            "action Morning garden check: tool run_command (a local-only tool)",
        ]
    )
    tools = {
        r["tool_name"]
        for r in await spark_db.fetch_all(
            "SELECT tool_name FROM global_tool_permissions WHERE user_guid = ?", (TARGET,)
        )
    }
    assert tools == {"web_fetch"}
    servers = {
        r["server_name"]
        for r in await spark_db.fetch_all(
            "SELECT server_name FROM conversation_mcp_servers WHERE user_guid = ?", (TARGET,)
        )
    }
    assert servers == {"remote-search"}
    action_tools = {
        r["tool_name"]
        for r in await spark_db.fetch_all(
            "SELECT tool_name FROM action_tool_permissions WHERE user_guid = ?", (TARGET,)
        )
    }
    assert action_tools == {"web_search"}


async def test_an_http_server_the_server_lacks_is_dropped(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store, mcp_servers=[]))
    assert "conversation MCP server remote-search (not offered on this server)" in report.dropped
    assert await _count(spark_db, "conversation_mcp_servers") == 0


async def test_files_are_rekeyed_into_the_target_area_and_charged(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, seeded = exported
    report = await _import(spark_db, data, _target(store))
    rows = await spark_db.fetch_all(
        "SELECT storage_key, filename, size_bytes, conversation_id FROM user_files "
        "WHERE user_guid = ? ORDER BY id",
        (TARGET,),
    )
    by_name = {r["filename"]: r for r in rows}
    for key, content in seeded.files.items():
        row = by_name[key.rsplit("/", 1)[1]]
        assert row["storage_key"].startswith(f"imports/{report.bundle_id}/")
        assert row["storage_key"].endswith("/files/" + key)
        with store.open(TARGET, row["storage_key"]) as stream:
            assert stream.read() == content
    plan = by_name["plan.pdf"]
    assert plan["conversation_id"] == report.ids["conversations"][seeded.conversations[0]]
    used = await spark_db.fetch_value(
        "SELECT bytes_used FROM user_file_usage WHERE user_guid = ?", (TARGET,)
    )
    assert used == sum(r["size_bytes"] for r in rows)


async def test_skills_are_kept_in_the_file_area_with_their_states(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    assert report.skills_placed == ["note-taker"]
    keys = [
        r["storage_key"]
        for r in await spark_db.fetch_all(
            "SELECT storage_key FROM user_files WHERE user_guid = ? AND storage_key LIKE ?",
            (TARGET, "%/skills/%"),
        )
    ]
    assert sorted(k.split("/skills/", 1)[1] for k in keys) == [
        "note-taker/SKILL.md",
        "note-taker/templates/meeting.txt",
    ]
    state = await spark_db.fetch_value(
        "SELECT enabled FROM skills WHERE user_guid = ? AND name = ?", (TARGET, "note-taker")
    )
    assert state == 0


async def test_preferences_fill_only_what_the_user_has_not_set(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    from spark.instance_settings import repository

    await repository.save_preference(spark_db, TARGET, "interface.theme", "dark")
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    prefs = await repository.load_preferences(spark_db, TARGET)
    assert prefs["interface.theme"] == "dark"
    assert prefs["default_model.model_id"] == "model-alpha"
    assert report.imported["preferences"] == 1


async def test_the_report_is_kept_as_a_receipt(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    key = f"imports/{report.bundle_id}/report.json"
    with store.open(TARGET, key) as stream:
        receipt = json.loads(stream.read())
    assert receipt["bundle_id"] == report.bundle_id
    assert receipt["imported"]["conversations"] == 2
    assert "ids" not in receipt


# -- Refusals --------------------------------------------------------------------------


async def test_reimporting_for_the_same_user_is_refused_and_changes_nothing(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    await _import(spark_db, data, _target(store))
    files_before = sorted(f.key for f in store.list(TARGET))
    with pytest.raises(ImportRefused, match="already imported"):
        await _import(spark_db, data, _target(store))
    assert await _count(spark_db, "conversations") == 2
    assert sorted(f.key for f in store.list(TARGET)) == files_before


async def test_the_same_bundle_may_be_imported_for_another_user(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    await _import(spark_db, data, _target(store))
    report = await _import(spark_db, data, _target(store, user="second-user"))
    assert report.imported["conversations"] == 2
    agent_ids = [r["agent_id"] for r in await spark_db.fetch_all("SELECT agent_id FROM agent_runs")]
    assert len(set(agent_ids)) == len(agent_ids) == 2


async def test_over_the_quota_nothing_is_written(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    with pytest.raises(ImportRefused, match="quota"):
        await _import(spark_db, data, _target(store, quota_bytes=10))
    assert await _count(spark_db, "conversations") == 0
    assert await _count(spark_db, "user_files") == 0
    assert store.list(TARGET) == []


async def test_a_failure_inside_the_transaction_rolls_everything_back(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported

    async def failing(conn: Any, report: Any) -> None:
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError):
        await _import(spark_db, data, _target(store), within=failing)
    assert await _count(spark_db, "conversations") == 0
    assert await _count(spark_db, "user_memories") == 0
    assert store.list(TARGET) == []


async def test_within_runs_in_the_transaction_with_the_finished_report(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    data, _ = exported
    seen: list[Any] = []

    async def record(conn: Any, report: Any) -> None:
        seen.append(
            (
                report.imported["conversations"],
                await conn.fetch_value(
                    "SELECT COUNT(*) FROM conversations WHERE user_guid = ?", (TARGET,)
                ),
            )
        )

    await _import(spark_db, data, _target(store), within=record)
    assert seen == [(2, 2)]


# -- Meeting the target's own data -----------------------------------------------------


async def test_clashes_with_the_targets_data_are_resolved_and_reported(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    import hashlib

    await ins(
        spark_db,
        "autonomous_actions",
        name="Morning garden check",
        action_prompt="mine",
        model_id="m",
        user_guid=TARGET,
    )
    content = "Has a shady garden bed"
    await ins(
        spark_db,
        "user_memories",
        user_guid=TARGET,
        content=content,
        category="facts",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    await ins(
        spark_db,
        "kg_nodes",
        scope="global",
        name="Kale",
        name_key="kale",
        weight=4,
        user_guid=TARGET,
    )
    data, _ = exported
    report = await _import(spark_db, data, _target(store))
    names = sorted(
        r["name"]
        for r in await spark_db.fetch_all(
            "SELECT name FROM autonomous_actions WHERE user_guid = ?", (TARGET,)
        )
    )
    assert names == ["Morning garden check", "Morning garden check (imported)"]
    assert report.actions_disabled == ["Morning garden check (imported)"]
    assert report.imported["user_memories"] == 1
    assert "1 memory the user already holds" in report.skipped
    weight = await spark_db.fetch_value(
        "SELECT weight FROM kg_nodes WHERE user_guid = ? AND scope = 'global'", (TARGET,)
    )
    assert weight == 7
    assert report.merged["kg_nodes"] == 1


async def test_rows_referring_to_data_not_in_the_bundle_are_skipped(
    spark_db: Any, store: Any
) -> None:
    out = io.BytesIO()
    with bf.BundleWriter(out, spark_version="x") as writer:
        writer.add_rows("conversations", [{"id": 1, "name": "kept"}])
        writer.add_rows(
            "messages",
            [
                {"id": 1, "conversation_id": 1, "role": "user", "content": "kept"},
                {"id": 2, "conversation_id": 99, "role": "user", "content": "orphan"},
            ],
        )
        writer.add_rows(
            "user_memories",
            [
                {
                    "id": 1,
                    "content": "m",
                    "category": "facts",
                    "content_hash": "h",
                    "source_conversation_id": 99,
                }
            ],
        )
    report = await _import(spark_db, out.getvalue(), _target(store))
    assert report.imported["messages"] == 1
    assert "1 messages row referring to data not in the bundle" in report.skipped
    # An optional reference to a missing row is cleared, not a reason to skip.
    assert (
        await spark_db.fetch_value(
            "SELECT source_conversation_id FROM user_memories WHERE user_guid = ?", (TARGET,)
        )
        is None
    )


@pytest.mark.postgres
@pytest.mark.parametrize("spark_db_settings", ["postgresql"], indirect=True)
async def test_vectors_are_searchable_on_postgresql(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any
) -> None:
    import numpy as np

    from spark.index.vectors import MEMORIES, vector_store
    from spark.mode import Dialect

    data, _ = exported
    await _import(spark_db, data, _target(store))
    query = np.frombuffer(vector(10), dtype=np.float32)
    rows = await vector_store(MEMORIES, Dialect.POSTGRESQL).search(
        spark_db, TARGET, query, top_k=1, threshold=0.9
    )
    assert [r["content"] for r in rows] == ["Has a shady garden bed"]
    assert await spark_db.fetch_value("SELECT COUNT(*) FROM context_vectors") == 1


async def test_only_user_preferences_are_imported(spark_db: Any, store: Any) -> None:
    from spark.instance_settings import repository

    out = io.BytesIO()
    with bf.BundleWriter(out, spark_version="x") as writer:
        writer.set_preferences(
            {
                "interface.theme": "light",
                "files.root": "/srv/elsewhere",
                "mcp.servers": [{"name": "x", "transport": "stdio"}],
                "interface.unknown": 1,
            }
        )
    report = await _import(spark_db, out.getvalue(), _target(store))
    assert await repository.load_preferences(spark_db, TARGET) == {"interface.theme": "light"}
    assert sorted(report.dropped) == [
        "setting files.root (not a user preference)",
        "setting interface.unknown (not a user preference)",
        "setting mcp.servers (not a user preference)",
    ]
    stored = await spark_db.fetch_value("SELECT COUNT(*) FROM instance_settings")
    assert stored == 0


async def test_a_concurrent_second_import_is_refused_by_the_receipt_claim(
    spark_db: Any, exported: tuple[bytes, Seeded], store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spark.bundles import importer

    data, _ = exported
    await _import(spark_db, data, _target(store))
    files_before = sorted(f.key for f in store.list(TARGET))

    async def not_yet(*args: Any) -> None:
        return None  # as a second import that checked before the first committed

    monkeypatch.setattr(importer.files_db, "get_user_file_by_key", not_yet)
    with pytest.raises(ImportRefused, match="already imported"):
        await _import(spark_db, data, _target(store))
    assert await _count(spark_db, "conversations") == 2
    assert sorted(f.key for f in store.list(TARGET)) == files_before
