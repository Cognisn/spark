"""The user bundle format, version 1: layout, the table allow-list and the writer.

A bundle is one zip archive (see ``docs/bundles.md``)::

    manifest.json              format, format_version, bundle_id, members (size, sha256), counts
    data/<table>.jsonl         one JSON object per row, for the tables in TABLES only
    files/<sha256>             the content of a file in the user's file area
    skills/<name>/<path>       the files of one of the user's own skills
    preferences.json           the user's preferences (user-scope settings only)

Rows carry the columns listed in :data:`TABLES` and nothing else: never a
``user_guid`` (an import assigns the target user) and never a column that is
not listed. A ``bytes`` value is written as ``{"$hex": "..."}``. Ids are the
exporting database's; an import maps them to new ids.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import IO, Any, Final

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "MANIFEST",
    "PREFERENCES",
    "TABLES",
    "BundleWriter",
    "data_member",
    "decode_value",
    "encode_value",
    "skill_member",
]

FORMAT: Final = "spark-user-bundle"
FORMAT_VERSION: Final = 1
MANIFEST: Final = "manifest.json"
PREFERENCES: Final = "preferences.json"

# Table -> the columns a bundle carries, in import order (a table after those it refers to).
TABLES: Final[Mapping[str, tuple[str, ...]]] = {
    "conversations": (
        "id",
        "name",
        "model_id",
        "created_at",
        "last_updated",
        "total_tokens",
        "is_active",
        "instructions",
        "tokens_sent",
        "tokens_received",
        "max_tokens",
        "compaction_threshold",
        "compaction_model",
        "compaction_summary_ratio",
        "web_search_enabled",
        "memory_enabled",
        "rag_enabled",
        "rag_top_k",
        "rag_threshold",
        "rag_tool_enabled",
        "max_history_messages",
        "include_tool_results",
        "prompt_caching",
        "is_favourite",
        "agents_enabled",
        "agent_mode",
        "agent_model_selection",
        "conversation_type",
        "kg_local_enabled",
        "kg_use_global",
        "kg_auto_context",
    ),
    "messages": (
        "id",
        "conversation_id",
        "role",
        "content",
        "token_count",
        "timestamp",
        "is_rolled_up",
    ),
    "rollup_history": (
        "id",
        "conversation_id",
        "original_message_count",
        "summarised_content",
        "original_token_count",
        "summarised_token_count",
        "rollup_timestamp",
    ),
    "conversation_files": (
        "id",
        "conversation_id",
        "filename",
        "file_type",
        "file_size",
        "content_text",
        "content_base64",
        "mime_type",
        "token_count",
        "added_at",
        "tags",
    ),
    "conversation_links": ("id", "source_conversation_id", "target_conversation_id", "created_at"),
    "conversation_model_usage": (
        "id",
        "conversation_id",
        "model_id",
        "input_tokens",
        "output_tokens",
        "first_used",
        "last_used",
    ),
    "conversation_tool_permissions": (
        "id",
        "conversation_id",
        "tool_name",
        "permission_state",
        "granted_at",
        "updated_at",
    ),
    "conversation_embedded_tools": ("id", "conversation_id", "tool_name", "enabled", "updated_at"),
    "conversation_mcp_servers": ("id", "conversation_id", "server_name", "enabled", "updated_at"),
    "conversation_skills": ("id", "conversation_id", "skill_name", "enabled"),
    "context_index_elements": (
        "id",
        "conversation_id",
        "element_type",
        "content_hash",
        "content_text",
        "embedding",
        "metadata_json",
        "created_at",
    ),
    "mcp_transactions": (
        "id",
        "conversation_id",
        "message_id",
        "user_prompt",
        "tool_name",
        "tool_server",
        "tool_input",
        "tool_response",
        "is_error",
        "execution_time_ms",
        "transaction_timestamp",
    ),
    "agent_runs": (
        "id",
        "agent_id",
        "parent_conversation_id",
        "agent_name",
        "task_description",
        "mode",
        "model_id",
        "status",
        "result_text",
        "tool_calls_json",
        "input_tokens",
        "output_tokens",
        "created_at",
        "completed_at",
    ),
    "debate_config": (
        "id",
        "conversation_id",
        "topic",
        "rounds_mode",
        "max_rounds",
        "state",
        "current_round",
        "opening_speaker",
        "created_at",
        "updated_at",
    ),
    "debate_agents": (
        "id",
        "conversation_id",
        "role",
        "model_id",
        "brief",
        "tokens_sent",
        "tokens_received",
        "allowed_tools",
        "allowed_skills",
        "display_name",
        "is_human",
        "voice_id",
    ),
    "debate_turns": (
        "id",
        "conversation_id",
        "round",
        "role",
        "turn_type",
        "content",
        "summary",
        "status",
        "token_count",
        "created_at",
    ),
    "debate_exhibits": ("id", "turn_id", "label", "title", "content", "source", "created_at"),
    "user_memories": (
        "id",
        "content",
        "category",
        "content_hash",
        "embedding",
        "importance",
        "created_at",
        "last_accessed",
        "source_conversation_id",
        "metadata_json",
    ),
    "kg_nodes": (
        "id",
        "scope",
        "name",
        "name_key",
        "entity_type",
        "description",
        "embedding",
        "weight",
        "created_at",
        "updated_at",
    ),
    "kg_edges": (
        "id",
        "scope",
        "source_node_id",
        "target_node_id",
        "relation",
        "description",
        "weight",
        "created_at",
        "relation_key",
    ),
    "kg_builds": ("id", "scope", "source_key", "watermark", "built_at", "status", "error"),
    "skills": ("id", "name", "enabled", "created_at", "updated_at"),
    "global_tool_permissions": ("id", "tool_name", "permission_state", "granted_at", "updated_at"),
    "autonomous_actions": (
        "id",
        "name",
        "description",
        "action_prompt",
        "model_id",
        "schedule_type",
        "schedule_config",
        "context_mode",
        "max_failures",
        "failure_count",
        "is_enabled",
        "max_tokens",
        "created_at",
        "last_run_at",
        "next_run_at",
        "version",
        "updated_at",
    ),
    "action_tool_permissions": (
        "id",
        "action_id",
        "tool_name",
        "server_name",
        "permission_state",
        "granted_at",
    ),
    "action_runs": (
        "id",
        "action_id",
        "started_at",
        "completed_at",
        "status",
        "result_text",
        "result_html",
        "error_message",
        "input_tokens",
        "output_tokens",
        "context_snapshot",
    ),
    # ``blob`` names the content (files/<sha256>), in place of the area's own path.
    "user_files": (
        "id",
        "storage_key",
        "filename",
        "size_bytes",
        "mime_type",
        "conversation_id",
        "source",
        "created_at",
        "updated_at",
        "blob",
    ),
}

SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# One component of a path inside a skill: plain names only, never hidden, never a device.
_SKILL_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9]|conin\$|conout\$|clock\$)(\..*)?$", re.I)
_MAX_SKILL_DEPTH = 8


def data_member(table: str) -> str:
    """The archive member holding ``table``'s rows."""
    if table not in TABLES:
        raise ValueError(f"{table!r} is not a bundle table")
    return f"data/{table}.jsonl"


def skill_path_parts(relative: str) -> list[str] | None:
    """The components of a path inside a skill, or None when it is not a plain relative path."""
    parts = relative.split("/")
    if not 1 <= len(parts) <= _MAX_SKILL_DEPTH:
        return None
    for part in parts:
        if not _SKILL_PART.match(part) or part.endswith(".") or _DEVICE.match(part):
            return None
    return parts


def skill_member(name: str, relative: str) -> str:
    """The archive member of one file of a skill; ValueError for an unsafe name or path."""
    if not SKILL_NAME.match(name) or len(name) > 64:
        raise ValueError("a skill name must be kebab-case")
    if skill_path_parts(relative) is None:
        raise ValueError("a skill file must be a plain relative path inside the skill")
    return f"skills/{name}/{relative}"


def encode_value(value: Any) -> Any:
    """A database value as JSON: ``bytes`` become ``{"$hex": ...}``."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$hex": bytes(value).hex()}
    return value


def decode_value(value: Any) -> Any:
    """The inverse of :func:`encode_value`."""
    if isinstance(value, dict):
        if set(value) == {"$hex"} and isinstance(value["$hex"], str):
            return bytes.fromhex(value["$hex"])
        raise ValueError('an object value must be {"$hex": ...}')
    return value


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BundleWriter:
    """Writes one bundle to a binary stream; the manifest is written on close."""

    def __init__(self, out: IO[bytes], *, spark_version: str) -> None:
        self._zip = zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED)
        self._members: dict[str, dict[str, Any]] = {}
        self._counts: dict[str, int] = {}
        self._meta: dict[str, Any] = {}
        self._manifest: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "bundle_id": uuid.uuid4().hex,
            "created_at": _now(),
            "spark_version": spark_version,
            "source_mode": "personal",
        }

    @property
    def bundle_id(self) -> str:
        return str(self._manifest["bundle_id"])

    def _write(self, name: str, data: bytes) -> None:
        if name in self._members:
            raise ValueError(f"{name} is already written")
        self._zip.writestr(name, data)
        self._members[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def add_rows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """Write ``table``'s rows (only its listed columns); returns how many."""
        member = data_member(table)
        columns = TABLES[table]
        lines = [
            json.dumps(
                {c: encode_value(row[c]) for c in columns if c in row},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for row in rows
        ]
        if lines:
            self._write(member, ("\n".join(lines) + "\n").encode("utf-8"))
            self._counts[table] = len(lines)
        return len(lines)

    def add_blob(self, data: bytes) -> str:
        """Write one file's content (once per distinct content); returns its sha256."""
        sha = hashlib.sha256(data).hexdigest()
        name = f"files/{sha}"
        if name not in self._members:
            self._write(name, data)
        return sha

    def add_skill_file(self, name: str, relative: str, data: bytes) -> None:
        self._write(skill_member(name, relative), data)

    def set_preferences(self, preferences: Mapping[str, Any]) -> None:
        self._write(PREFERENCES, json.dumps(dict(preferences), sort_keys=True).encode("utf-8"))

    def set_meta(self, key: str, value: Any) -> None:
        """An extra manifest entry (for example the MCP transports a bundle refers to)."""
        self._meta[key] = value

    def close(self) -> dict[str, Any]:
        manifest = {
            **self._meta,
            **self._manifest,
            "counts": dict(self._counts),
            "members": dict(sorted(self._members.items())),
        }
        self._zip.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
        self._zip.close()
        return manifest

    def __enter__(self) -> BundleWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
        else:
            self._zip.close()
