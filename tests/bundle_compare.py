"""Test-only: compare a personal user's rows with what an import made of them, row for row.

Rows of each table are matched in id order (an import keeps each table's
order), after leaving out the rows a server cannot run. Every listed column of
every table is compared, with references followed through the id maps this
builds, and with the changes an import makes on purpose applied to the source
side: actions disabled and unlocked, knowledge graph scopes and watermarks
following the new ids, files re-keyed under ``imports/<bundle>/``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np

from spark.bundles import format as bf
from spark.bundles.exporter import _SCOPES
from spark.bundles.importer import _REFERENCES

# What the seed holds that a server (offering remote-search over http) cannot run.
LOCAL_ONLY = {"run_command", "read_file", "filesystem"}
MCP_TOOLS = {"notes__search"}
OFFERED_MCP = {"remote-search"}


def kept(table: str, row: dict[str, Any]) -> bool:
    if table in ("conversation_tool_permissions", "global_tool_permissions"):
        return row["tool_name"] not in LOCAL_ONLY | MCP_TOOLS
    if table == "conversation_embedded_tools":
        return row["tool_name"] not in LOCAL_ONLY
    if table == "conversation_mcp_servers":
        return row["server_name"] in OFFERED_MCP
    if table == "action_tool_permissions":
        if row["server_name"]:
            return row["server_name"] in OFFERED_MCP
        return row["tool_name"] not in LOCAL_ONLY
    return True


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def norm(value: Any) -> Any:
    """One representation per value: timestamps as naive UTC text (SQLite keeps them as
    written, the engine returns PostgreSQL's as ISO text with an offset)."""
    if isinstance(value, str) and _TIMESTAMP.match(value):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


async def _rows(db: Any, table: str, user: str, columns: list[str]) -> list[dict[str, Any]]:
    where = _SCOPES.get(table, "user_guid = ?")
    rows = await db.fetch_all(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where} ORDER BY id", (user,)
    )
    return [{k: norm(v) for k, v in r.items()} for r in rows]


def _scope(value: Any, conversations: dict[int, int]) -> Any:
    if isinstance(value, str) and value.startswith("conv:"):
        return f"conv:{conversations[int(value[5:])]}"
    return value


async def compare(
    source: Any, source_user: str, target: Any, target_user: str, bundle_id: str
) -> dict[str, int]:
    """Assert every kept row arrived unchanged (apart from intended changes); count per table."""
    ids: dict[str, dict[int, int]] = {}
    message_conversation: dict[int, int] = {}
    compared: dict[str, int] = {}
    for table, all_columns in bf.TABLES.items():
        columns = [c for c in all_columns if c != "blob"]
        src = [r for r in await _rows(source, table, source_user, columns) if kept(table, r)]
        dst = await _rows(target, table, target_user, columns)
        if table == "user_files":
            dst = [r for r in dst if "/files/" in r["storage_key"]]
        assert len(src) == len(dst), f"{table}: {len(src)} source rows, {len(dst)} imported"
        ids[table] = {s["id"]: d["id"] for s, d in zip(src, dst, strict=True)}
        if table == "messages":
            message_conversation = {r["id"]: r["conversation_id"] for r in src}
        for s, d in zip(src, dst, strict=True):
            expected = dict(s)
            for (ref_table, column), (to, _) in _REFERENCES.items():
                if ref_table == table and expected.get(column) is not None:
                    expected[column] = ids[to].get(expected[column])
            if table == "autonomous_actions":
                expected.update(is_enabled=0)
            if table.startswith("kg_"):
                expected["scope"] = _scope(expected["scope"], ids["conversations"])
            if table == "kg_builds":
                key = s["source_key"]
                expected["source_key"] = _scope(key, ids["conversations"])
                if key == "memories":
                    pool = ids["user_memories"]
                else:
                    conv = int(key[5:])
                    pool = {
                        o: n for o, n in ids["messages"].items() if message_conversation[o] == conv
                    }
                expected["watermark"] = max(
                    (n for o, n in pool.items() if o <= s["watermark"]), default=0
                )
            if table == "user_files":
                assert d["storage_key"].startswith(f"imports/{bundle_id}/")
                assert d["storage_key"].endswith("/files/" + s["storage_key"])
                expected["storage_key"] = d["storage_key"]
            expected["id"] = d["id"]
            diff = {k: (expected.get(k), d.get(k)) for k in d if d.get(k) != expected.get(k)}
            assert not diff, f"{table} row {s['id']}: (expected, imported) {diff}"
        compared[table] = len(src)
    action_locks = await target.fetch_all(
        "SELECT locked_by, locked_at FROM autonomous_actions WHERE user_guid = ?", (target_user,)
    )
    assert all(r["locked_by"] is None and r["locked_at"] is None for r in action_locks)
    return compared


async def compare_vectors(source: Any, source_user: str, target: Any, target_user: str) -> int:
    """Each imported memory and context element has its source vector in pgvector."""
    checked = 0
    for table, vectors, key in (
        ("user_memories", "memory_vectors", "memory_id"),
        ("context_index_elements", "context_vectors", "element_id"),
    ):
        src = await source.fetch_all(
            f"SELECT embedding FROM {table} WHERE user_guid = ? ORDER BY id", (source_user,)
        )
        dst = await target.fetch_all(
            f"SELECT v.embedding FROM {vectors} v JOIN {table} t ON t.id = v.{key} "
            "WHERE t.user_guid = ? ORDER BY t.id",
            (target_user,),
        )
        assert len(src) == len(dst)
        for s, d in zip(src, dst, strict=True):
            expected = np.frombuffer(bytes(s["embedding"]), dtype=np.float32)
            assert np.array_equal(np.asarray(d["embedding"], dtype=np.float32), expected)
            checked += 1
    return checked
