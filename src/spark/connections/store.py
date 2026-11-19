"""Where connections are kept: the database on a server, ``config.yaml`` in personal mode.

:class:`ConnectionStore` is the one interface both modes implement:

* :class:`DatabaseConnectionStore` (server mode) over the ``connections``,
  ``connection_models`` and ``catalogue_overlays`` tables
  (:mod:`spark.database.connections`), whose writes notify the other web replicas;
* :class:`FileConnectionStore` (personal mode) over the ``connections`` list of
  ``config.yaml``, each entry holding its models and its catalogue overlay entries
  (``overlays``). Writes rewrite the file with LF line endings, one writer at a
  time.

Each catalogue overlay entry belongs to the connection that declared it, in both
modes, and is applied to that connection's catalogue only.

Neither stores a credential: a connection's settings hold ``secret://``
references only, refused otherwise before anything is written (A9). Metering and
assignments are server mode only, kept in the database (M3).

:func:`load_snapshot` reads everything the connection runtime needs in one go: the
connections, their models and their overlay entries from the store, and (on a
server) the assignments from the database. In personal mode it first moves any
overlay entry the database still holds from before entries were scoped into the
``config.yaml`` entries of the connections that offered it
(:func:`~spark.connections.catalogue.offering_connections`), and removes it from
the database.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

import yaml

from spark.connections.catalogue import Candidate, catalogue_provider, offering_connections
from spark.connections.model import (
    TIERS,
    Connection,
    ConnectionInvalid,
    ConnectionModel,
    residency_record,
)
from spark.connections.runtime import Snapshot, snapshot_of
from spark.database import connections as repo
from spark.db.engine import Executor

__all__ = [
    "SECTION",
    "ConnectionStore",
    "DatabaseConnectionStore",
    "FileConnectionStore",
    "load_snapshot",
]

logger = logging.getLogger(__name__)

#: The ``config.yaml`` section personal mode keeps its connections in.
SECTION = "connections"
#: The key of a connection's catalogue overlay entries in its ``config.yaml`` entry.
OVERLAYS = "overlays"
_SCOPES = frozenset({"local", "in_region", "cross_region", "global"})


class ConnectionStore(Protocol):
    """Connections and the models each offers, in whichever place the mode keeps them."""

    async def list_connections(self) -> list[Connection]: ...

    async def get_connection(self, connection_id: str) -> Connection | None: ...

    async def save_connection(self, connection: Connection) -> None: ...

    async def delete_connection(self, connection_id: str) -> bool: ...

    async def list_models(self, connection_id: str | None = None) -> list[ConnectionModel]: ...

    async def save_model(self, model: ConnectionModel) -> None: ...

    async def list_overlays(self, connection_id: str | None = None) -> list[repo.OverlayRow]: ...

    async def save_overlay(self, entry: repo.OverlayRow) -> None: ...


def _plaintext_check(connection: Connection) -> None:
    found = repo.plaintext_credentials(connection.settings)
    if found:
        raise ConnectionInvalid(
            "a connection credential must be a secret:// reference: " + ", ".join(found)
        )


# -- Server mode ----------------------------------------------------------------------


def _from_row(row: repo.ConnectionRow) -> Connection:
    return Connection.from_record(
        {
            "id": row.id,
            "name": row.name,
            "settings": row.settings,
            "residency": row.residency,
            "enabled": row.enabled,
        }
    )


class DatabaseConnectionStore:
    """Server mode's connections, in the database."""

    def __init__(self, db: Executor) -> None:
        self._db = db

    async def list_connections(self) -> list[Connection]:
        return [_from_row(r) for r in await repo.list_connections(self._db)]

    async def get_connection(self, connection_id: str) -> Connection | None:
        row = await repo.get_connection(self._db, connection_id)
        return _from_row(row) if row is not None else None

    async def save_connection(self, connection: Connection) -> None:
        _plaintext_check(connection)
        await repo.save_connection(
            self._db,
            repo.ConnectionRow(
                id=connection.id,
                name=connection.name,
                kind=connection.kind,
                settings=dict(connection.settings),
                residency=residency_record(connection.residency),
                enabled=connection.enabled,
            ),
        )

    async def delete_connection(self, connection_id: str) -> bool:
        return await repo.delete_connection(self._db, connection_id)

    async def list_models(self, connection_id: str | None = None) -> list[ConnectionModel]:
        rows = await repo.list_models(self._db, connection_id)
        return [ConnectionModel(r.connection_id, r.model_id, r.enabled, r.tier) for r in rows]

    async def save_model(self, model: ConnectionModel) -> None:
        await repo.save_model(
            self._db, repo.ModelRow(model.connection_id, model.model_id, model.enabled, model.tier)
        )

    async def list_overlays(self, connection_id: str | None = None) -> list[repo.OverlayRow]:
        return await repo.list_overlays(self._db, connection_id)

    async def save_overlay(self, entry: repo.OverlayRow) -> None:
        await repo.save_overlay(self._db, entry)


# -- Overlay entries in config.yaml ---------------------------------------------------


def _overlay_record(entry: repo.OverlayRow) -> dict[str, Any]:
    return {
        "provider": entry.provider,
        "model_id": entry.model_id,
        "kind": entry.kind,
        "context_window": entry.context_window,
        "output_limit": entry.output_limit,
        "embedding_dimensions": entry.embedding_dimensions,
        "tools": entry.tools,
        "vision": entry.vision,
        "prompt_caching": entry.prompt_caching,
        "structured_output": entry.structured_output,
        "residency_scope": entry.residency_scope,
        "regions": sorted(entry.regions),
    }


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _overlay_from(record: Mapping[str, Any], connection_id: str) -> repo.OverlayRow | None:
    """The entry a ``config.yaml`` record holds, or None for one that cannot be read."""
    try:
        regions = record.get("regions") or []
        return repo.OverlayRow(
            provider=str(record["provider"]),
            model_id=str(record["model_id"]),
            kind="embedding" if record.get("kind") == "embedding" else "chat",
            context_window=int(record["context_window"]),
            output_limit=_optional_int(record.get("output_limit")),
            embedding_dimensions=_optional_int(record.get("embedding_dimensions")),
            tools=bool(record.get("tools", False)),
            vision=bool(record.get("vision", False)),
            prompt_caching=bool(record.get("prompt_caching", False)),
            structured_output=bool(record.get("structured_output", False)),
            residency_scope=str(record["residency_scope"]),
            regions=tuple(sorted(str(r) for r in regions)),
            connection_id=connection_id,
        )
    except (KeyError, TypeError, ValueError):
        return None


# -- Personal mode --------------------------------------------------------------------


class FileConnectionStore:
    """Personal mode's connections, in ``config.yaml``'s ``connections`` list."""

    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        with self._locks_guard:
            self._lock = self._locks.setdefault(self._path.resolve(), threading.Lock())

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, raw: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(raw, fh, default_flow_style=False, sort_keys=False)

    @staticmethod
    def _entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
        entries = raw.get(SECTION)
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def read_connections(self) -> list[Connection]:
        """The stored connections (an entry that cannot be read is skipped)."""
        found: list[Connection] = []
        for entry in self._entries(self._read()):
            try:
                found.append(Connection.from_record(entry))
            except ConnectionInvalid:
                continue
        return sorted(found, key=lambda c: c.id)

    def read_models(self, connection_id: str | None = None) -> list[ConnectionModel]:
        models: list[ConnectionModel] = []
        for entry in self._entries(self._read()):
            cid = str(entry.get("id", ""))
            if connection_id is not None and cid != connection_id:
                continue
            for item in entry.get("models") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                tier = item.get("tier", "standard")
                models.append(
                    ConnectionModel(
                        cid,
                        str(item["id"]),
                        bool(item.get("enabled", True)),
                        tier if tier in TIERS else "standard",
                    )
                )
        return models

    def has_models(self, connection_id: str) -> bool:
        """Whether the connection's entry holds a models list (it is curated)."""
        for entry in self._entries(self._read()):
            if entry.get("id") == connection_id:
                return isinstance(entry.get("models"), list)
        return False

    def write_connections(self, connections: list[Connection]) -> None:
        """Add or replace connections, keeping each entry's models."""
        with self._lock:
            raw = self._read()
            entries = self._entries(raw)
            by_id = {str(e.get("id")): e for e in entries}
            for connection in connections:
                _plaintext_check(connection)
                record = connection.to_record()
                old = by_id.get(connection.id)
                for kept in ("models", OVERLAYS):
                    if old is not None and kept in old:
                        record[kept] = old[kept]
                by_id[connection.id] = record
            raw[SECTION] = [by_id[k] for k in sorted(by_id)]
            self._write(raw)

    def _delete(self, connection_id: str) -> bool:
        with self._lock:
            raw = self._read()
            entries = self._entries(raw)
            kept = [e for e in entries if e.get("id") != connection_id]
            if len(kept) == len(entries):
                return False
            raw[SECTION] = kept
            self._write(raw)
            return True

    def _save_model(self, model: ConnectionModel) -> None:
        with self._lock:
            raw = self._read()
            entries = self._entries(raw)
            for entry in entries:
                if entry.get("id") != model.connection_id:
                    continue
                models = [
                    m
                    for m in (entry.get("models") or [])
                    if isinstance(m, dict) and m.get("id") != model.model_id
                ]
                models.append({"id": model.model_id, "enabled": model.enabled, "tier": model.tier})
                entry["models"] = sorted(models, key=lambda m: str(m["id"]))
                raw[SECTION] = entries
                self._write(raw)
                return
            raise ConnectionInvalid(f"no connection {model.connection_id!r}")

    def read_overlays(self, connection_id: str | None = None) -> list[repo.OverlayRow]:
        """The overlay entries of one connection, or of every connection (an entry that
        cannot be read is skipped), by provider, model and connection."""
        found: list[repo.OverlayRow] = []
        for entry in self._entries(self._read()):
            cid = str(entry.get("id", ""))
            if connection_id is not None and cid != connection_id:
                continue
            for item in entry.get(OVERLAYS) or []:
                row = _overlay_from(item, cid) if isinstance(item, Mapping) else None
                if row is not None:
                    found.append(row)
        return sorted(found, key=lambda o: (o.provider, o.model_id, o.connection_id or ""))

    def write_overlays(self, entries: list[repo.OverlayRow]) -> None:
        """Add or replace overlay entries, each in its own connection's entry (one per
        connection, provider and model). Refuses an entry of no connection, or of a
        connection that is not stored, before anything is written."""
        with self._lock:
            raw = self._read()
            connections = self._entries(raw)
            by_id = {str(e.get("id")): e for e in connections}
            for entry in entries:
                if not entry.connection_id:
                    raise ConnectionInvalid(
                        "an overlay entry belongs to a connection: give its connection_id"
                    )
                if entry.connection_id not in by_id:
                    raise ConnectionInvalid(f"no connection {entry.connection_id!r}")
                if entry.residency_scope not in _SCOPES:
                    raise ConnectionInvalid(
                        "a residency scope is local, in_region, cross_region or global"
                    )
            for entry in entries:
                target = by_id[str(entry.connection_id)]
                kept = [
                    o
                    for o in (target.get(OVERLAYS) or [])
                    if isinstance(o, dict)
                    and (o.get("provider"), o.get("model_id")) != (entry.provider, entry.model_id)
                ]
                kept.append(_overlay_record(entry))
                target[OVERLAYS] = sorted(
                    kept, key=lambda o: (str(o.get("provider")), str(o.get("model_id")))
                )
            raw[SECTION] = connections
            self._write(raw)

    async def list_connections(self) -> list[Connection]:
        return await asyncio.to_thread(self.read_connections)

    async def get_connection(self, connection_id: str) -> Connection | None:
        for connection in await self.list_connections():
            if connection.id == connection_id:
                return connection
        return None

    async def save_connection(self, connection: Connection) -> None:
        await asyncio.to_thread(self.write_connections, [connection])

    async def delete_connection(self, connection_id: str) -> bool:
        return await asyncio.to_thread(self._delete, connection_id)

    async def list_models(self, connection_id: str | None = None) -> list[ConnectionModel]:
        return await asyncio.to_thread(self.read_models, connection_id)

    async def save_model(self, model: ConnectionModel) -> None:
        await asyncio.to_thread(self._save_model, model)

    async def list_overlays(self, connection_id: str | None = None) -> list[repo.OverlayRow]:
        return await asyncio.to_thread(self.read_overlays, connection_id)

    async def save_overlay(self, entry: repo.OverlayRow) -> None:
        await asyncio.to_thread(self.write_overlays, [entry])


async def _move_unscoped_overlays(db: Executor, store: FileConnectionStore) -> None:
    """Personal mode: move the database's overlay entries of no connection (stored
    before entries were scoped) into the connections that offered them, then remove
    them from the database. An entry no connection offered is removed, logged by key."""
    unscoped = [o for o in await repo.list_overlays(db) if o.connection_id is None]
    if not unscoped:
        return
    connections = await store.list_connections()
    choices: dict[str, dict[str, bool]] = {}
    for m in await store.list_models():
        choices.setdefault(m.connection_id, {})[m.model_id] = m.enabled
    candidates = [
        Candidate(
            c.id,
            catalogue_provider(c.settings),
            choices.get(c.id, {}) if c.id in choices or store.has_models(c.id) else None,
        )
        for c in connections
    ]
    for entry in unscoped:
        owners = offering_connections(entry, candidates)
        if owners:
            await asyncio.to_thread(
                store.write_overlays, [replace(entry, connection_id=cid) for cid in owners]
            )
            logger.info(
                "Catalogue overlay entry %s:%s moved to connection(s) %s",
                entry.provider,
                entry.model_id,
                ", ".join(owners),
            )
        else:
            logger.info(
                "Catalogue overlay entry %s:%s belongs to no connection; removed",
                entry.provider,
                entry.model_id,
            )
        await repo.delete_overlay(db, entry.provider, entry.model_id, connection_id=None)


async def load_snapshot(db: Executor, store: ConnectionStore, *, server: bool) -> Snapshot:
    """Everything the connection runtime applies, read now."""
    if isinstance(store, FileConnectionStore):
        await _move_unscoped_overlays(db, store)
    connections = await store.list_connections()
    models = await store.list_models()
    curated = {m.connection_id for m in models}
    if isinstance(store, FileConnectionStore):
        curated |= {c.id for c in connections if store.has_models(c.id)}
    assignments = await repo.assignments(db) if server else []
    overlays = await store.list_overlays()
    snapshot = snapshot_of(connections, models, assignments, overlays)
    # A connection with a stored (possibly empty) model list is curated: only its
    # enabled models are offered. One without is offered its catalogue models.
    grouped = {cid: snapshot.models.get(cid, ()) for cid in curated}
    return Snapshot(
        connections=snapshot.connections,
        models=grouped,
        assignments=snapshot.assignments,
        overlays=snapshot.overlays,
    )
