"""Catalogue overlay entries scoped to a connection: ``catalogue_overlays.connection_id``.

Revision ID: overlay_connection_scope
Revises: credit_metering

Before this revision an overlay entry was keyed by catalogue provider and model id,
so a model declared on one connection (a Foundry deployment, a model pulled into
one Ollama) was offered on every connection of the same catalogue. Each entry now
belongs to the connection that declared it, and the connection runtime applies an
entry to that connection's catalogue only.

The upgrade rebuilds the table (SQLite cannot change a primary key) with a nullable
``connection_id`` referencing ``connections`` (``ON DELETE CASCADE``), unique per
connection, provider and model, and copies each existing entry to every connection
that would offer it today, one row per connection, so no connection loses a model
it offers:

* the connection's catalogue provider is the entry's provider (the connection's
  kind; for an OpenAI-compatible connection its service, or for a Foundry service
  the catalogue it names, ``openai`` by default);
* a chat entry goes to a connection with no stored model choices (it offers every
  catalogue model) or with the model stored and enabled; an embedding entry goes to
  every connection of the provider;
* the connection's residency policy is not consulted: it still decides at run time,
  exactly as before.

An entry no connection would offer (personal mode's, whose connections live in
``config.yaml``, or one whose connection is gone) keeps a NULL ``connection_id``,
unique per provider and model, and applies to no connection. Personal mode moves
those into the matching ``config.yaml`` entries at its next start
(:func:`spark.connections.store.load_snapshot`).

The downgrade restores one entry per provider and model. While one model is
declared differently on two connections it refuses, changing nothing, rather than
choose one declaration: make them agree, or delete one, first.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "overlay_connection_scope"
down_revision = "credit_metering"
branch_labels = None
depends_on = None

_TABLE = "catalogue_overlays"
_ASIDE = "catalogue_overlays_previous"

#: Every column of an entry, apart from its connection.
_ENTRY = (
    "provider",
    "model_id",
    "kind",
    "context_window",
    "output_limit",
    "embedding_dimensions",
    "tools",
    "vision",
    "prompt_caching",
    "structured_output",
    "residency_scope",
    "regions",
)
_COLUMNS = (*_ENTRY, "updated_at")

_DEFINITION = """CREATE TABLE {table} (
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'embedding')),
            context_window INTEGER NOT NULL CHECK (context_window > 0),
            output_limit INTEGER,
            embedding_dimensions INTEGER,
            tools INTEGER NOT NULL DEFAULT 0,
            vision INTEGER NOT NULL DEFAULT 0,
            prompt_caching INTEGER NOT NULL DEFAULT 0,
            structured_output INTEGER NOT NULL DEFAULT 0,
            residency_scope TEXT NOT NULL
                CHECK (residency_scope IN ('local', 'in_region', 'cross_region', 'global')),
            regions TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL{extra}
        )"""

_SCOPED_EXTRA = ",\n            connection_id TEXT REFERENCES connections(id) ON DELETE CASCADE"
_UNSCOPED_EXTRA = ",\n            PRIMARY KEY (provider, model_id)"

_INDEXES = (
    "CREATE UNIQUE INDEX uq_catalogue_overlays_connection "
    "ON catalogue_overlays (connection_id, provider, model_id)",
    "CREATE UNIQUE INDEX uq_catalogue_overlays_unscoped "
    "ON catalogue_overlays (provider, model_id) WHERE connection_id IS NULL",
)


def catalogue_provider(kind: str, settings: Mapping[str, Any]) -> str:
    """The catalogue provider of a connection's settings, as conduit 0.1.0 derives it
    (a frozen copy: this revision must not change when the application does)."""
    if kind != "openai_compat":
        return kind
    service = settings.get("service")
    if not isinstance(service, Mapping):
        return kind
    name = service.get("name")
    if name == "foundry":
        return str(service.get("catalogue_provider") or "openai")
    return str(name) if name else kind


def assigned_connections(
    entry: Mapping[str, Any],
    connections: Sequence[Mapping[str, Any]],
    models: Sequence[Mapping[str, Any]],
) -> list[str]:
    """The ids of the connections that offer ``entry`` before this revision, by id."""
    curated: set[str] = set()
    enabled: set[tuple[str, str]] = set()
    for row in models:
        cid = str(row["connection_id"])
        curated.add(cid)
        if row["enabled"]:
            enabled.add((cid, str(row["model_id"])))
    found: list[str] = []
    for connection in connections:
        cid = str(connection["id"])
        raw = connection["settings"]
        settings = json.loads(raw) if isinstance(raw, str) else raw
        if catalogue_provider(str(connection["kind"]), settings or {}) != entry["provider"]:
            continue
        if entry["kind"] == "chat" and cid in curated:
            if (cid, str(entry["model_id"])) not in enabled:
                continue
        found.append(cid)
    return sorted(found)


def _rows(sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in op.get_bind().execute(sa.text(sql)).mappings().all()]


def _insert(table: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    names = ", ".join(columns)
    marks = ", ".join(f":{c}" for c in columns)
    op.get_bind().execute(
        sa.text(f"INSERT INTO {table} ({names}) VALUES ({marks})"),
        [{c: row[c] for c in columns} for row in rows],
    )


def upgrade() -> None:
    entries = _rows(f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE}")
    connections = _rows("SELECT id, kind, settings FROM connections ORDER BY id")
    models = _rows("SELECT connection_id, model_id, enabled FROM connection_models")
    op.execute(f"ALTER TABLE {_TABLE} RENAME TO {_ASIDE}")
    op.execute(_DEFINITION.format(table=_TABLE, extra=_SCOPED_EXTRA))
    for statement in _INDEXES:
        op.execute(statement)
    scoped: list[dict[str, Any]] = []
    for entry in entries:
        owners: list[str | None] = list(assigned_connections(entry, connections, models))
        for cid in owners or [None]:
            scoped.append({**entry, "connection_id": cid})
    _insert(_TABLE, (*_COLUMNS, "connection_id"), scoped)
    op.execute(f"DROP TABLE {_ASIDE}")


def _refuse_differing_declarations(entries: Sequence[Mapping[str, Any]]) -> None:
    seen: dict[tuple[str, str], tuple[Any, ...]] = {}
    differing: set[tuple[str, str]] = set()
    for entry in entries:
        key = (str(entry["provider"]), str(entry["model_id"]))
        fields = tuple(entry[c] for c in _ENTRY)
        if seen.setdefault(key, fields) != fields:
            differing.add(key)
    if differing:
        raise RuntimeError(
            f"cannot downgrade {revision}: {len(differing)} model(s) are declared differently "
            "on two or more connections, and before this revision a model had one declaration "
            "for every connection of its catalogue; make those declarations agree, or delete "
            "all but one, first (nothing was changed)"
        )


def downgrade() -> None:
    entries = _rows(
        f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} "
        "ORDER BY provider, model_id, COALESCE(connection_id, '')"
    )
    _refuse_differing_declarations(entries)
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        kept.setdefault((str(entry["provider"]), str(entry["model_id"])), entry)
    op.execute("DROP INDEX uq_catalogue_overlays_unscoped")
    op.execute("DROP INDEX uq_catalogue_overlays_connection")
    op.execute(f"ALTER TABLE {_TABLE} RENAME TO {_ASIDE}")
    op.execute(_DEFINITION.format(table=_TABLE, extra=_UNSCOPED_EXTRA))
    _insert(_TABLE, _COLUMNS, list(kept.values()))
    op.execute(f"DROP TABLE {_ASIDE}")
