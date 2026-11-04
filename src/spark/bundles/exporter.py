"""Exporting one personal-mode user's data as a bundle (A15).

:func:`export_bundle` reads the user's rows of every table in
:data:`spark.bundles.format.TABLES` (rows reached through a conversation or a
debate turn are scoped through their owner), the contents of the user's file
area, the user's own skill folders and their preferences, and writes a bundle.

What never leaves:

* any other user's rows, and the owner's guid itself;
* instance configuration: providers, MCP servers (only the transport of a server
  the data refers to is written, so an import can drop stdio references), tool
  policy and every other non-user setting; only user-scope, non-secret
  preferences are written;
* usage records, voice usage, prompt inspection violations and the daemon
  registry, which are the instance's accounting and security records;
* secrets: every text value has the instance's secret values and anything shaped
  like a credential replaced (:func:`spark.bundles.secrets.redact`), and a file
  or binary skill file holding one is left out and listed in the report.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from spark.bundles import format as bf
from spark.bundles import secrets
from spark.db.engine import Executor, Row
from spark.files.store import FileStore
from spark.instance_settings import registry

__all__ = ["ExportReport", "ExportSources", "export_bundle", "user_preferences"]

_OWNED = "SELECT id FROM conversations WHERE user_guid = ?"
# Tables without a user_guid column, scoped through their owner.
_SCOPES: Mapping[str, str] = {
    "conversation_skills": f"conversation_id IN ({_OWNED})",
    "debate_agents": f"conversation_id IN ({_OWNED})",
    "debate_turns": f"conversation_id IN ({_OWNED})",
    "debate_exhibits": (
        f"turn_id IN (SELECT id FROM debate_turns WHERE conversation_id IN ({_OWNED}))"
    ),
}
_SKILL_FILE_MAX = 16 << 20


@dataclass(frozen=True, slots=True)
class ExportSources:
    """What an export reads besides the database."""

    files: FileStore | None
    skills_dir: Path | None
    preferences: Mapping[str, Any]
    mcp_servers: Sequence[Mapping[str, Any]]
    secret_values: Iterable[str] = ()


@dataclass(slots=True)
class ExportReport:
    bundle_id: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    redactions: int = 0
    files_left_out: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skills_left_out: list[str] = field(default_factory=list)


def user_preferences(values: Mapping[str, Any]) -> dict[str, Any]:
    """``values`` limited to user-scope, non-secret settings (A4)."""
    allowed = {k for k in registry.keys(registry.Scope.USER) if not registry.spec(k).secret}
    return {k: v for k, v in values.items() if k in allowed}


class _Redactor:
    def __init__(self, secret_values: Iterable[str]) -> None:
        self.values = [v for v in secret_values if v]
        self._raw = [v.encode("utf-8") for v in self.values if len(v) >= 8]
        self.count = 0

    def text(self, value: str) -> str:
        cleaned, hits = secrets.redact(value, self.values)
        self.count += hits
        return cleaned

    def row(self, row: Row) -> dict[str, Any]:
        return {k: self.text(v) if isinstance(v, str) else v for k, v in row.items()}

    def holds_secret(self, data: bytes) -> bool:
        return bool(secrets.find(data)) or any(raw in data for raw in self._raw)


async def _rows(db: Executor, table: str, user_guid: str) -> list[Row]:
    columns = [c for c in bf.TABLES[table] if c != "blob"]
    where = _SCOPES.get(table, "user_guid = ?")
    return await db.fetch_all(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where} ORDER BY id", (user_guid,)
    )


def _read(store: FileStore, user_guid: str, key: str) -> bytes | None:
    try:
        with store.open(user_guid, key) as stream:
            return stream.read()
    except (FileNotFoundError, OSError, ValueError):
        return None


async def _file_rows(
    db: Executor,
    user_guid: str,
    store: FileStore | None,
    writer: bf.BundleWriter,
    redactor: _Redactor,
    report: ExportReport,
) -> list[dict[str, Any]]:
    out = []
    for row in await _rows(db, "user_files", user_guid):
        key = str(row["storage_key"])
        data = None if store is None else await asyncio.to_thread(_read, store, user_guid, key)
        if data is None:
            report.files_left_out.append(f"{key} (missing from the file area)")
            continue
        if redactor.holds_secret(data):
            report.files_left_out.append(f"{key} (holds a credential)")
            continue
        cleaned = redactor.row(row)
        cleaned["blob"] = writer.add_blob(data)
        out.append(cleaned)
    return out


def _skill_files(
    skills_dir: Path | None, redactor: _Redactor, report: ExportReport
) -> list[tuple[str, str, bytes]]:
    if skills_dir is None or not skills_dir.is_dir():
        return []
    out: list[tuple[str, str, bytes]] = []
    for folder in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        name = folder.name
        if folder.is_symlink() or not bf.SKILL_NAME.match(name) or len(name) > 64:
            report.skills_left_out.append(f"{name} (not a valid skill name)")
            continue
        if not (folder / "SKILL.md").is_file():
            report.skills_left_out.append(f"{name} (no SKILL.md)")
            continue
        report.skills.append(name)
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder).as_posix()
            if path.is_dir() and not path.is_symlink():
                continue
            label = f"{name}/{relative}"
            if path.is_symlink() or not path.is_file():
                report.skills_left_out.append(f"{label} (not a regular file)")
            elif bf.skill_path_parts(relative) is None:
                report.skills_left_out.append(f"{label} (not a plain path)")
            elif path.stat().st_size > _SKILL_FILE_MAX:
                report.skills_left_out.append(f"{label} (larger than 16 MB)")
            else:
                data = path.read_bytes()
                text = None if b"\0" in data else _utf8(data)
                if text is not None:
                    out.append((name, relative, redactor.text(text).encode("utf-8")))
                elif redactor.holds_secret(data):
                    report.skills_left_out.append(f"{label} (holds a credential)")
                else:
                    out.append((name, relative, data))
    return out


def _utf8(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _transports(
    rows: Mapping[str, list[dict[str, Any]]], servers: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """The transport of each MCP server the rows refer to (``unknown`` when not configured)."""
    known = {str(s.get("name")): str(s.get("transport") or "unknown") for s in servers}
    names = {str(r["server_name"]) for r in rows.get("conversation_mcp_servers", [])}
    names |= {
        str(r["server_name"]) for r in rows.get("action_tool_permissions", []) if r["server_name"]
    }
    return {name: known.get(name, "unknown") for name in sorted(names)}


async def export_bundle(
    db: Executor,
    user_guid: str,
    out: IO[bytes],
    sources: ExportSources,
    *,
    spark_version: str,
) -> ExportReport:
    """Write ``user_guid``'s bundle to ``out``; returns what was written and left out."""
    report = ExportReport()
    redactor = _Redactor(sources.secret_values)
    with bf.BundleWriter(out, spark_version=spark_version) as writer:
        tables: dict[str, list[dict[str, Any]]] = {}
        for table in bf.TABLES:
            if table == "user_files":
                tables[table] = await _file_rows(
                    db, user_guid, sources.files, writer, redactor, report
                )
                continue
            rows = [redactor.row(r) for r in await _rows(db, table, user_guid)]
            if table == "user_memories":
                for row in rows:
                    row["content_hash"] = hashlib.sha256(row["content"].encode()).hexdigest()
            tables[table] = rows
        for table, rows in tables.items():
            count = writer.add_rows(table, rows)
            if count:
                report.counts[table] = count
        skill_files = await asyncio.to_thread(_skill_files, sources.skills_dir, redactor, report)
        for name, relative, data in skill_files:
            writer.add_skill_file(name, relative, data)
        preferences = {
            k: redactor.text(v) if isinstance(v, str) else v
            for k, v in user_preferences(sources.preferences).items()
        }
        if preferences:
            writer.set_preferences(preferences)
        writer.set_meta("mcp_transports", _transports(tables, sources.mcp_servers))
        report.bundle_id = writer.bundle_id
    report.redactions = redactor.count
    return report
