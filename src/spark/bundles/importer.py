"""Importing a checked bundle for one user of a server (A15).

:func:`import_bundle` writes a bundle's rows for the target user in one
transaction, with every id mapped to a new one, and returns an
:class:`ImportReport`. The file contents and skill files are written into the
target's file area first, under ``imports/<bundle id>/<attempt>/``, and removed
again if anything fails; their rows are charged to the user's quota in the same
transaction (:func:`spark.database.files.record_user_files`).

What changes on the way in:

* every scheduled action arrives disabled, its lock cleared; a name the user
  already uses gets `` (imported)``;
* what a server cannot run is dropped and listed: approvals and per-conversation
  settings for local-only tools (A6) and MCP tools, per-conversation MCP servers
  whose transport is stdio or which this server does not offer as HTTP or SSE,
  and the same in an action's tool permissions;
* the target's own data wins: a memory or an "Always" approval the user already
  holds, a skill state or a preference already set, are kept and the bundle's
  skipped; knowledge graph nodes and edges with the same natural key merge (their
  weights add up);
* a row referring to a row the bundle lacks is skipped (a nullable reference is
  cleared instead);
* skills are kept in the file area under ``.../skills/<name>/`` and listed:
  a server's skills are instance-wide (Q26);
* on PostgreSQL, imported memory and context vectors are added to their pgvector
  tables.

A bundle is imported once per user: the report is written to
``imports/<bundle id>/report.json`` in the user's area in the same transaction,
so a second import of the bundle for that user is refused (D49).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spark.bundles import format as bf
from spark.bundles.reader import Bundle
from spark.database import files as files_db
from spark.db.engine import Connection, Executor
from spark.files.paths import PathRefused
from spark.files.store import FileStore
from spark.index.vectors import CONTEXT, MEMORIES, EmbeddingDimensionMismatch, vector_store
from spark.instance_settings import registry, repository
from spark.mode import Dialect
from spark.tools.registry import LOCAL_ONLY_CATEGORIES, TOOL_CATEGORIES, tool_category

__all__ = ["ImportRefused", "ImportReport", "ImportTarget", "import_bundle"]

Within = Callable[[Connection, "ImportReport"], Awaitable[None]]

# Tables without a user_guid column (scoped through their conversation or turn).
_NO_OWNER = frozenset({"conversation_skills", "debate_agents", "debate_turns", "debate_exhibits"})
# (table, column) -> (the table it refers to, whether a missing row clears it instead).
_REFERENCES: Mapping[tuple[str, str], tuple[str, bool]] = {
    **{
        (table, "conversation_id"): ("conversations", False)
        for table in (
            "messages",
            "rollup_history",
            "conversation_files",
            "conversation_model_usage",
            "conversation_tool_permissions",
            "conversation_embedded_tools",
            "conversation_mcp_servers",
            "conversation_skills",
            "context_index_elements",
            "mcp_transactions",
            "debate_config",
            "debate_agents",
            "debate_turns",
        )
    },
    ("conversation_links", "source_conversation_id"): ("conversations", False),
    ("conversation_links", "target_conversation_id"): ("conversations", False),
    ("mcp_transactions", "message_id"): ("messages", True),
    ("agent_runs", "parent_conversation_id"): ("conversations", False),
    ("debate_exhibits", "turn_id"): ("debate_turns", False),
    ("user_memories", "source_conversation_id"): ("conversations", True),
    ("kg_edges", "source_node_id"): ("kg_nodes", False),
    ("kg_edges", "target_node_id"): ("kg_nodes", False),
    ("action_tool_permissions", "action_id"): ("autonomous_actions", False),
    ("action_runs", "action_id"): ("autonomous_actions", False),
    ("user_files", "conversation_id"): ("conversations", True),
}
_SERVER_TRANSPORTS = frozenset({"http", "sse"})
_RECEIPT = "report.json"


class ImportRefused(Exception):
    """The import changed nothing; the message says why."""


@dataclass(frozen=True, slots=True)
class ImportTarget:
    """Who receives the bundle and what this server offers."""

    user_guid: str
    store: FileStore
    quota_bytes: int | None
    mcp_servers: Sequence[Mapping[str, Any]]


@dataclass(slots=True)
class ImportReport:
    """What an import did; :meth:`summary` is what the administrator and the receipt see."""

    bundle_id: str
    format_version: int
    imported: dict[str, int] = field(default_factory=dict)
    merged: dict[str, int] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    actions_disabled: list[str] = field(default_factory=list)
    skills_placed: list[str] = field(default_factory=list)
    # Old id -> new id per table (for tests and diagnostics; never shown or stored).
    ids: dict[str, dict[int, int]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "format_version": self.format_version,
            "imported": dict(self.imported),
            "merged": dict(self.merged),
            "dropped": list(self.dropped),
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
            "actions_disabled": list(self.actions_disabled),
            "skills_placed": list(self.skills_placed),
        }

    @property
    def total_rows(self) -> int:
        return sum(v for k, v in self.imported.items() if k != "preferences")


@dataclass(slots=True)
class _Files:
    """Content written to the area for this attempt, and the rows to record for it."""

    prefix: str
    written: list[str] = field(default_factory=list)
    rows: list[tuple[files_db.NewUserFile, dict[str, Any]]] = field(default_factory=list)


def _tool_drop_reason(tool: str) -> str | None:
    """Why a server cannot honour a setting for ``tool``, or None when it can."""
    if tool in LOCAL_ONLY_CATEGORIES or tool_category(tool) in LOCAL_ONLY_CATEGORIES:
        return "a local-only tool"
    if tool in TOOL_CATEGORIES or tool_category(tool) is not None:
        return None
    return "an MCP tool"


class _Run:
    """One import's state: the id maps, the report and the connection."""

    def __init__(
        self, conn: Connection, bundle: Bundle, target: ImportTarget, report: ImportReport
    ) -> None:
        self.conn = conn
        self.bundle = bundle
        self.user = target.user_guid
        self.report = report
        transports = bundle.manifest.get("mcp_transports")
        self.transports: dict[str, str] = (
            {str(k): str(v) for k, v in transports.items()} if isinstance(transports, dict) else {}
        )
        self.offered = {
            str(s.get("name"))
            for s in target.mcp_servers
            if str(s.get("transport")) in _SERVER_TRANSPORTS
        }
        self.orphans: dict[str, int] = {}
        self._message_conversation: dict[int, int] = {}

    # -- helpers ------------------------------------------------------------------

    def ids(self, table: str) -> dict[int, int]:
        return self.report.ids.setdefault(table, {})

    def count(self, table: str, n: int = 1) -> None:
        self.report.imported[table] = self.report.imported.get(table, 0) + n

    def _mcp_reason(self, server: str) -> str | None:
        transport = self.transports.get(server, "unknown")
        if transport == "stdio":
            return "stdio, which a server does not run"
        if server not in self.offered:
            return "not offered on this server"
        return None

    def remap(self, table: str, row: dict[str, Any]) -> dict[str, Any] | None:
        """``row`` with its references mapped, or None when a required one is missing."""
        out = dict(row)
        for (ref_table, column), (target, optional) in _REFERENCES.items():
            if ref_table != table or out.get(column) is None:
                continue
            new = self.report.ids.get(target, {}).get(int(out[column]))
            if new is None:
                if not optional:
                    self.orphans[table] = self.orphans.get(table, 0) + 1
                    return None
            out[column] = new
        return out

    def scope(self, scope: Any) -> str | None:
        """A knowledge graph scope with its conversation mapped (None: not in the bundle)."""
        text = str(scope)
        if not text.startswith("conv:"):
            return text
        try:
            old = int(text.split(":", 1)[1])
        except ValueError:
            return None
        new = self.ids("conversations").get(old)
        return None if new is None else f"conv:{new}"

    async def insert(self, table: str, row: dict[str, Any], *, conflict: str = "") -> int | None:
        values = {k: v for k, v in row.items() if k not in ("id", "blob")}
        if table not in _NO_OWNER:
            values["user_guid"] = self.user
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        new = await self.conn.fetch_value(
            f"INSERT INTO {table} ({names}) VALUES ({marks}) {conflict} RETURNING id",
            tuple(values.values()),
        )
        if new is None:
            return None
        if "id" in row:
            self.ids(table)[int(row["id"])] = int(new)
        return int(new)

    # -- tables -------------------------------------------------------------------

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        return self.bundle.rows(table)

    async def plain(self, table: str) -> None:
        for row in self.rows(table):
            mapped = self.remap(table, row)
            if mapped is not None and await self.insert(table, mapped) is not None:
                self.count(table)

    async def conversation_tools(self) -> None:
        for row in self.rows("conversation_tool_permissions"):
            reason = _tool_drop_reason(str(row.get("tool_name")))
            if reason:
                self.report.dropped.append(
                    f"conversation approval of {row.get('tool_name')} ({reason})"
                )
                continue
            await self._insert_mapped("conversation_tool_permissions", row)
        for row in self.rows("conversation_embedded_tools"):
            if _tool_drop_reason(str(row.get("tool_name"))) == "a local-only tool":
                self.report.dropped.append(
                    f"conversation tool {row.get('tool_name')} (a local-only tool)"
                )
                continue
            await self._insert_mapped("conversation_embedded_tools", row)
        for row in self.rows("conversation_mcp_servers"):
            reason = self._mcp_reason(str(row.get("server_name")))
            if reason:
                self.report.dropped.append(
                    f"conversation MCP server {row.get('server_name')} ({reason})"
                )
                continue
            await self._insert_mapped("conversation_mcp_servers", row)

    async def _insert_mapped(self, table: str, row: dict[str, Any], conflict: str = "") -> None:
        mapped = self.remap(table, row)
        if mapped is not None and await self.insert(table, mapped, conflict=conflict) is not None:
            self.count(table)

    async def agent_runs(self) -> None:
        for row in self.rows("agent_runs"):
            mapped = self.remap("agent_runs", row)
            if mapped is None:
                continue
            taken = await self.conn.fetch_value(
                "SELECT 1 FROM agent_runs WHERE agent_id = ?", (mapped.get("agent_id"),)
            )
            if taken or not mapped.get("agent_id"):
                mapped["agent_id"] = f"agent-{uuid.uuid4().hex}"
            if await self.insert("agent_runs", mapped) is not None:
                self.count("agent_runs")

    async def memories(self, dialect: Dialect) -> None:
        skipped = 0
        for row in self.rows("user_memories"):
            mapped = self.remap("user_memories", row)
            if mapped is None:
                continue
            new = await self.insert(
                "user_memories", mapped, conflict="ON CONFLICT (user_guid, content_hash) DO NOTHING"
            )
            if new is None:
                skipped += 1
                continue
            self.count("user_memories")
            await self.vector(dialect, MEMORIES, new, mapped.get("embedding"))
        if skipped:
            noun = "memory" if skipped == 1 else "memories"
            self.report.skipped.append(f"{skipped} {noun} the user already holds")

    async def context(self, dialect: Dialect) -> None:
        for row in self.rows("context_index_elements"):
            mapped = self.remap("context_index_elements", row)
            if mapped is None:
                continue
            new = await self.insert("context_index_elements", mapped)
            if new is not None:
                self.count("context_index_elements")
                await self.vector(dialect, CONTEXT, new, mapped.get("embedding"))

    async def vector(self, dialect: Dialect, collection: Any, item: int, blob: Any) -> None:
        if dialect is not Dialect.POSTGRESQL or not isinstance(blob, bytes) or not blob:
            return
        if len(blob) % 4:
            self.report.warnings.append(f"a {collection.name} vector could not be read")
            return
        vec = np.frombuffer(blob, dtype=np.float32)
        try:
            await vector_store(collection, dialect).add(self.conn, self.user, item, vec)
        except EmbeddingDimensionMismatch:
            self.report.warnings.append(
                f"a {collection.name} vector has another width than the user's; "
                "rebuild the user's index to search it"
            )

    async def knowledge(self) -> None:
        merged = 0
        for row in self.rows("kg_nodes"):
            scope = self.scope(row.get("scope"))
            if scope is None:
                self.orphans["kg_nodes"] = self.orphans.get("kg_nodes", 0) + 1
                continue
            existing = await self.conn.fetch_value(
                "SELECT id FROM kg_nodes WHERE user_guid = ? AND scope = ? AND name_key = ?",
                (self.user, scope, row.get("name_key")),
            )
            await self.insert(
                "kg_nodes",
                {**row, "scope": scope},
                conflict="ON CONFLICT (user_guid, scope, name_key) DO UPDATE "
                "SET weight = kg_nodes.weight + excluded.weight",
            )
            if existing is not None:
                merged += 1
            else:
                self.count("kg_nodes")
        for row in self.rows("kg_edges"):
            mapped = self.remap("kg_edges", row)
            scope = self.scope(row.get("scope"))
            if mapped is None or scope is None:
                if mapped is not None:
                    self.orphans["kg_edges"] = self.orphans.get("kg_edges", 0) + 1
                continue
            await self.insert(
                "kg_edges",
                {**mapped, "scope": scope},
                conflict="ON CONFLICT (user_guid, scope, source_node_id, target_node_id, "
                "relation_key) DO UPDATE SET weight = kg_edges.weight + excluded.weight",
            )
            self.count("kg_edges")
        if merged:
            self.report.merged["kg_nodes"] = merged
        for row in self.rows("kg_builds"):
            scope = self.scope(row.get("scope"))
            source = self._source_key(row.get("source_key"))
            if scope is None or source is None:
                self.orphans["kg_builds"] = self.orphans.get("kg_builds", 0) + 1
                continue
            mark = self._watermark(str(row.get("source_key")), row.get("watermark"))
            await self.insert(
                "kg_builds", {**row, "scope": scope, "source_key": source, "watermark": mark}
            )
            self.count("kg_builds")

    def _source_key(self, key: Any) -> str | None:
        return self.scope(key) if str(key).startswith("conv:") else str(key)

    def _watermark(self, source_key: str, watermark: Any) -> int:
        """The new id of the last row a build had seen (ids keep their order on import)."""
        if not isinstance(watermark, int):
            return 0
        if source_key == "memories":
            ids = self.ids("user_memories")
        elif source_key.startswith("conv:"):
            ids = self._conversation_messages(source_key)
        else:
            return watermark
        seen = [new for old, new in ids.items() if old <= watermark]
        return max(seen, default=0)

    def _conversation_messages(self, source_key: str) -> dict[int, int]:
        old_conv = int(source_key.split(":", 1)[1])
        return {
            old: new
            for old, new in self.ids("messages").items()
            if self._message_conversation.get(old) == old_conv
        }

    async def messages(self) -> None:
        for row in self.rows("messages"):
            mapped = self.remap("messages", row)
            if mapped is not None and await self.insert("messages", mapped) is not None:
                self.count("messages")
                self._message_conversation[int(row["id"])] = int(row["conversation_id"])

    async def skills(self) -> None:
        kept = 0
        for row in self.rows("skills"):
            new = await self.insert(
                "skills", row, conflict="ON CONFLICT (user_guid, name) DO NOTHING"
            )
            if new is None:
                kept += 1
            else:
                self.count("skills")
        if kept:
            self.report.skipped.append(f"{kept} skill states the user had already set")

    async def approvals(self) -> None:
        kept = 0
        for row in self.rows("global_tool_permissions"):
            reason = _tool_drop_reason(str(row.get("tool_name")))
            if reason:
                self.report.dropped.append(
                    f"\"Always\" approval of {row.get('tool_name')} ({reason})"
                )
                continue
            new = await self.insert(
                "global_tool_permissions",
                row,
                conflict="ON CONFLICT (user_guid, tool_name) DO NOTHING",
            )
            if new is None:
                kept += 1
            else:
                self.count("global_tool_permissions")
        if kept:
            self.report.skipped.append(f'{kept} "Always" approvals the user already holds')

    async def actions(self) -> None:
        names: dict[int, str] = {}
        for row in self.rows("autonomous_actions"):
            name = await self._free_action_name(str(row.get("name") or "Imported action"))
            values = {
                **row,
                "name": name,
                "is_enabled": 0,
                "locked_by": None,
                "locked_at": None,
            }
            if await self.insert("autonomous_actions", values) is not None:
                self.count("autonomous_actions")
                self.report.actions_disabled.append(name)
                names[int(row["id"])] = name
        for row in self.rows("action_tool_permissions"):
            label = names.get(int(row.get("action_id") or 0), "?")
            server = row.get("server_name")
            tool = str(row.get("tool_name"))
            if server:
                reason = self._mcp_reason(str(server))
                if reason:
                    self.report.dropped.append(
                        f"action {label}: tool {tool} of MCP server {server} ({reason})"
                    )
                    continue
            else:
                tool_reason = _tool_drop_reason(tool)
                if tool_reason:
                    self.report.dropped.append(f"action {label}: tool {tool} ({tool_reason})")
                    continue
            await self._insert_mapped("action_tool_permissions", row)
        await self.plain("action_runs")

    async def _free_action_name(self, name: str) -> str:
        candidate, n = name, 1
        while await self.conn.fetch_value(
            "SELECT 1 FROM autonomous_actions WHERE user_guid = ? AND name = ?",
            (self.user, candidate),
        ):
            n += 1
            candidate = f"{name} (imported)" if n == 2 else f"{name} (imported {n - 1})"
        if candidate != name:
            self.report.warnings.append(
                f"the action {name} was renamed {candidate}: the user already has one so named"
            )
        return candidate

    async def preferences(self) -> None:
        stored = await repository.load_preferences(self.conn, self.user)
        allowed = {k for k in registry.keys(registry.Scope.USER) if not registry.spec(k).secret}
        for key, value in sorted(self.bundle.preferences().items()):
            if key not in allowed:
                self.report.dropped.append(f"setting {key} (not a user preference)")
                continue
            if key in stored:
                continue
            try:
                await repository.save_preference(self.conn, self.user, key, value)
            except registry.SettingsError:
                self.report.dropped.append(f"preference {key} (not a valid value here)")
                continue
            self.count("preferences")

    def finish(self) -> None:
        for table, n in sorted(self.orphans.items()):
            self.report.skipped.append(f"{n} {table} row referring to data not in the bundle")


def _put(store: FileStore, user: str, key: str, data: bytes) -> None:
    store.put(user, key, data)


async def _write_files(bundle: Bundle, target: ImportTarget, files: _Files) -> None:
    """Write the bundle's file contents and skill files into this attempt's folder of the area."""
    for row in bundle.rows("user_files"):
        key = f"{files.prefix}/files/{row.get('storage_key')}"
        data = bundle.blob(str(row.get("blob")))
        try:
            await asyncio.to_thread(_put, target.store, target.user_guid, key, data)
        except (PathRefused, ValueError, OSError):
            continue
        files.written.append(key)
        new = files_db.NewUserFile(
            key=key,
            filename=str(row.get("filename") or key.rsplit("/", 1)[1]),
            size=len(data),
            mime_type=row.get("mime_type"),
            conversation_id=row.get("conversation_id"),
            source=str(row.get("source") or "upload"),
        )
        files.rows.append((new, row))
    for skill, relative, data in bundle.skill_files():
        key = f"{files.prefix}/skills/{skill}/{relative}"
        await asyncio.to_thread(_put, target.store, target.user_guid, key, data)
        files.written.append(key)
        files.rows.append(
            (
                files_db.NewUserFile(
                    key=key, filename=relative.rsplit("/", 1)[-1], size=len(data), source="import"
                ),
                {"skill": skill},
            )
        )


async def _record_files(run: _Run, files: _Files, target: ImportTarget) -> None:
    new_files: list[files_db.NewUserFile] = []
    for new, row in files.rows:
        if "skill" in row:
            new_files.append(new)
            continue
        conversation = row.get("conversation_id")
        mapped = None if conversation is None else run.ids("conversations").get(int(conversation))
        new_files.append(
            files_db.NewUserFile(
                key=new.key,
                filename=new.filename,
                size=new.size,
                mime_type=new.mime_type,
                conversation_id=mapped,
                source=new.source,
            )
        )
    receipt = json.dumps(run.report.summary(), indent=2, sort_keys=True).encode("utf-8")
    receipt_key = f"imports/{run.report.bundle_id}/{_RECEIPT}"
    await asyncio.to_thread(_put, target.store, target.user_guid, receipt_key, receipt)
    files.written.append(receipt_key)
    new_files.append(
        files_db.NewUserFile(key=receipt_key, filename=_RECEIPT, size=len(receipt), source="import")
    )
    try:
        ids = await files_db.record_user_files(
            run.conn, run.user, new_files, quota_bytes=target.quota_bytes
        )
    except files_db.QuotaExceeded as exc:
        raise ImportRefused(
            "the bundle's files would take the user over their storage quota "
            f"({exc.used + exc.requested} of {exc.quota} bytes)"
        ) from None
    for file_id, (new, row) in zip(ids, files.rows, strict=False):
        if "skill" in row:
            continue
        run.ids("user_files")[int(row["id"])] = file_id
        await run.conn.execute(
            "UPDATE user_files SET created_at = COALESCE(?, created_at), "
            "updated_at = COALESCE(?, updated_at) WHERE id = ? AND user_guid = ?",
            (row.get("created_at"), row.get("updated_at"), file_id, run.user),
        )
        run.count("user_files")
    run.report.skills_placed = sorted({str(r["skill"]) for _, r in files.rows if "skill" in r})


def _remove(store: FileStore, user: str, keys: list[str]) -> None:
    for key in keys:
        try:
            store.delete(user, key)
        except (OSError, ValueError):
            pass


async def import_bundle(
    db: Executor,
    bundle: Bundle,
    target: ImportTarget,
    *,
    within: Within | None = None,
) -> ImportReport:
    """Import ``bundle`` for ``target.user_guid``; see the module docstring.

    ``within`` runs inside the transaction once the report is complete (the
    audit record of the import); if it raises, nothing is imported.
    Raises :class:`ImportRefused` (nothing changed) for a bundle already
    imported for this user or one over the user's quota.
    """
    report = ImportReport(
        bundle_id=bundle.bundle_id, format_version=int(bundle.manifest["format_version"])
    )
    receipt_key = f"imports/{bundle.bundle_id}/{_RECEIPT}"
    if await files_db.get_user_file_by_key(db, target.user_guid, receipt_key) is not None:
        raise ImportRefused("this bundle was already imported for this user")
    files = _Files(prefix=f"imports/{bundle.bundle_id}/{uuid.uuid4().hex[:12]}")
    try:
        await _write_files(bundle, target, files)
        async with db.transaction() as conn:
            # Claim the receipt first: a concurrent import of the same bundle for this user
            # waits on this row (PostgreSQL) or the write lock (SQLite), then finds it taken.
            claimed = await conn.fetch_value(
                "INSERT INTO user_files (user_guid, storage_key, filename, size_bytes, source) "
                "VALUES (?, ?, ?, 0, 'import') ON CONFLICT DO NOTHING RETURNING id",
                (target.user_guid, receipt_key, _RECEIPT),
            )
            if claimed is None:
                raise ImportRefused("this bundle was already imported for this user")
            run = _Run(conn, bundle, target, report)
            await run.plain("conversations")
            await run.messages()
            for table in (
                "rollup_history",
                "conversation_files",
                "conversation_links",
                "conversation_model_usage",
            ):
                await run.plain(table)
            await run.conversation_tools()
            await run.plain("conversation_skills")
            await run.context(conn.dialect)
            await run.plain("mcp_transactions")
            await run.agent_runs()
            for table in ("debate_config", "debate_agents", "debate_turns", "debate_exhibits"):
                await run.plain(table)
            await run.memories(conn.dialect)
            await run.knowledge()
            await run.skills()
            await run.approvals()
            await run.actions()
            await run.preferences()
            run.finish()
            await _record_files(run, files, target)
            if within is not None:
                await within(conn, report)
    except BaseException:
        await asyncio.to_thread(_remove, target.store, target.user_guid, files.written)
        raise
    return report
