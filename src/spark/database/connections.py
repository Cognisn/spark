"""Connections, their models, SPARK's catalogue overlay, metering and assignments (M3 to M10).

Server mode keeps its named connections here (personal mode keeps them in
``config.yaml``, :class:`spark.connections.store.FileConnectionStore`). A
connection's ``settings`` are its conduit provider settings as JSON, every
credential a ``secret://`` reference: this module refuses to store a settings
mapping whose credential-like field holds anything else, whatever the caller
checked (A9).

Each write to a connection, its models, its metering or its assignments, and to
an overlay entry, tells the other web replicas on PostgreSQL (``NOTIFY`` on the
instance settings channel, payload :data:`NOTICE`), inside the write's
transaction, so their connection runtimes refresh.

Connections and the catalogue overlay are instance-wide and deliberately not
scoped by user: only an administrator's endpoint and the connection runtime reach
them. Each overlay entry belongs to the connection that declared it
(``connection_id``) and is applied to that connection's catalogue only; an entry
with no connection is one stored before entries were scoped that no connection
offered when they were (it applies to none). Assignments name users; :func:`connections_for` answers for one user and
their roles.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from spark.db.dialect import upsert
from spark.db.engine import Executor, Row
from spark.mode import Dialect

__all__ = [
    "NOTICE",
    "TIERS",
    "Assignment",
    "ConnectionRow",
    "Metering",
    "ModelRow",
    "OverlayRow",
    "assignments",
    "connections_for",
    "delete_assignment",
    "delete_connection",
    "delete_model",
    "delete_overlay",
    "get_connection",
    "get_metering",
    "list_connections",
    "list_models",
    "list_overlays",
    "plaintext_credentials",
    "save_assignment",
    "save_connection",
    "save_metering",
    "save_model",
    "save_overlay",
]

#: The payload SPARK sends on the instance settings channel after a connection write.
NOTICE: Final = "connections"
_CHANNEL: Final = "spark_instance_settings"

Tier = Literal["lower", "standard", "higher"]
TIERS: Final[frozenset[str]] = frozenset({"lower", "standard", "higher"})
_SCOPES: Final = frozenset({"local", "in_region", "cross_region", "global"})
_SECRET_PREFIX: Final = "secret://"
_CREDENTIAL_SEGMENTS: Final = frozenset(
    {"key", "apikey", "secret", "token", "password", "passphrase", "credential", "credentials"}
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _notify(db: Executor) -> None:
    if db.dialect is Dialect.POSTGRESQL:
        await db.execute("SELECT pg_notify(?, ?)", (_CHANNEL, NOTICE))


def _credential_name(name: str) -> bool:
    return any(part in _CREDENTIAL_SEGMENTS for part in name.lower().replace("-", "_").split("_"))


def plaintext_credentials(value: Any, path: str = "") -> list[str]:
    """Paths under ``value`` whose credential-like field holds anything but a reference."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if _credential_name(str(key)) and isinstance(item, str):
                if not item.startswith(_SECRET_PREFIX):
                    found.append(where)
            else:
                found.extend(plaintext_credentials(item, where))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.extend(plaintext_credentials(item, f"{path}.{index}"))
    return found


# -- Connections ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionRow:
    """One stored connection. ``settings`` and ``residency`` are mappings."""

    id: str
    name: str
    kind: str
    settings: Mapping[str, Any]
    residency: Mapping[str, Any]
    enabled: bool = True


def _connection(row: Row) -> ConnectionRow:
    return ConnectionRow(
        id=str(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        settings=cast(dict[str, Any], json.loads(str(row["settings"]))),
        residency=cast(dict[str, Any], json.loads(str(row["residency"]))),
        enabled=bool(row["enabled"]),
    )


_SAVE_CONNECTION = upsert(
    "connections",
    ["id", "name", "kind", "settings", "residency", "enabled", "created_at", "updated_at"],
    ["id"],
    update=["name", "kind", "settings", "residency", "enabled", "updated_at"],
)


async def list_connections(db: Executor) -> list[ConnectionRow]:
    """Every connection, by id. Deliberately unscoped: instance-wide configuration."""
    rows = await db.fetch_all(
        "SELECT id, name, kind, settings, residency, enabled FROM connections ORDER BY id"
    )
    return [_connection(r) for r in rows]


async def get_connection(db: Executor, connection_id: str) -> ConnectionRow | None:
    """One connection, or None. Deliberately unscoped: instance-wide configuration."""
    row = await db.fetch_one(
        "SELECT id, name, kind, settings, residency, enabled FROM connections WHERE id = ?",
        (connection_id,),
    )
    return _connection(row) if row is not None else None


async def save_connection(db: Executor, connection: ConnectionRow) -> None:
    """Insert or replace a connection. Refuses a plain-text credential by field name."""
    plaintext = plaintext_credentials(connection.settings)
    if plaintext:
        raise ValueError(
            "a connection credential must be a secret:// reference: " + ", ".join(plaintext)
        )
    if connection.settings.get("kind") != connection.kind:
        raise ValueError("a connection's kind must match its settings' kind")
    now = _now()
    async with db.transaction() as conn:
        await conn.execute(
            _SAVE_CONNECTION,
            (
                connection.id,
                connection.name,
                connection.kind,
                json.dumps(connection.settings, sort_keys=True),
                json.dumps(connection.residency, sort_keys=True),
                1 if connection.enabled else 0,
                now,
                now,
            ),
        )
        await _notify(conn)


async def delete_connection(db: Executor, connection_id: str) -> bool:
    """Remove a connection with its models, metering and assignments. True if it existed."""
    async with db.transaction() as conn:
        for table in (
            "catalogue_overlays",
            "connection_assignments",
            "connection_metering",
            "connection_models",
        ):
            await conn.execute(f"DELETE FROM {table} WHERE connection_id = ?", (connection_id,))
        removed = await conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
        await _notify(conn)
    return removed > 0


# -- Models a connection offers -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelRow:
    """A model on a connection: enabled or not, and its credit tier."""

    connection_id: str
    model_id: str
    enabled: bool = True
    tier: Tier = "standard"


_SAVE_MODEL = upsert(
    "connection_models",
    ["connection_id", "model_id", "enabled", "tier", "updated_at"],
    ["connection_id", "model_id"],
)


def _tier(value: object) -> Tier:
    if value not in TIERS:
        raise ValueError("a tier is lower, standard or higher")
    return cast(Tier, value)


async def list_models(db: Executor, connection_id: str | None = None) -> list[ModelRow]:
    """The models of one connection, or of every connection, by connection and model."""
    where, params = ("WHERE connection_id = ?", (connection_id,)) if connection_id else ("", ())
    rows = await db.fetch_all(
        f"SELECT connection_id, model_id, enabled, tier FROM connection_models {where} "
        "ORDER BY connection_id, model_id",
        params,
    )
    return [
        ModelRow(str(r["connection_id"]), str(r["model_id"]), bool(r["enabled"]), _tier(r["tier"]))
        for r in rows
    ]


async def save_model(db: Executor, model: ModelRow) -> None:
    """Enable or disable a model on a connection and set its tier."""
    async with db.transaction() as conn:
        await conn.execute(
            _SAVE_MODEL,
            (
                model.connection_id,
                model.model_id,
                1 if model.enabled else 0,
                _tier(model.tier),
                _now(),
            ),
        )
        await _notify(conn)


async def delete_model(db: Executor, connection_id: str, model_id: str) -> bool:
    """Forget a model on a connection. True if it was stored."""
    async with db.transaction() as conn:
        removed = await conn.execute(
            "DELETE FROM connection_models WHERE connection_id = ? AND model_id = ?",
            (connection_id, model_id),
        )
        await _notify(conn)
    return removed > 0


# -- SPARK's catalogue overlay --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverlayRow:
    """One overlay entry on conduit's catalogue: the fields of a conduit ``ModelEntry``,
    and the connection whose catalogue it belongs to (None only for an entry stored
    before entries were scoped, which applies to no connection)."""

    provider: str
    model_id: str
    context_window: int
    residency_scope: str
    kind: Literal["chat", "embedding"] = "chat"
    output_limit: int | None = None
    embedding_dimensions: int | None = None
    tools: bool = False
    vision: bool = False
    prompt_caching: bool = False
    structured_output: bool = False
    regions: tuple[str, ...] = ()
    connection_id: str | None = None


_OVERLAY_COLUMNS: Final = (
    "provider, model_id, kind, context_window, output_limit, embedding_dimensions, "
    "tools, vision, prompt_caching, structured_output, residency_scope, regions, "
    "connection_id"
)


def _overlay(row: Row) -> OverlayRow:
    def _optional(value: object) -> int | None:
        return None if value is None else int(cast(int, value))

    kind = str(row["kind"])
    return OverlayRow(
        provider=str(row["provider"]),
        model_id=str(row["model_id"]),
        kind="embedding" if kind == "embedding" else "chat",
        context_window=int(row["context_window"]),
        output_limit=_optional(row["output_limit"]),
        embedding_dimensions=_optional(row["embedding_dimensions"]),
        tools=bool(row["tools"]),
        vision=bool(row["vision"]),
        prompt_caching=bool(row["prompt_caching"]),
        structured_output=bool(row["structured_output"]),
        residency_scope=str(row["residency_scope"]),
        regions=tuple(str(r) for r in json.loads(str(row["regions"]))),
        connection_id=None if row["connection_id"] is None else str(row["connection_id"]),
    )


async def list_overlays(db: Executor, connection_id: str | None = None) -> list[OverlayRow]:
    """The overlay entries of one connection, or every entry (with those of no
    connection), by provider, model and connection. Deliberately unscoped by user:
    instance-wide configuration."""
    where, params = ("WHERE connection_id = ?", (connection_id,)) if connection_id else ("", ())
    rows = await db.fetch_all(
        f"SELECT {_OVERLAY_COLUMNS} FROM catalogue_overlays {where} "
        "ORDER BY provider, model_id, COALESCE(connection_id, '')",
        params,
    )
    return [_overlay(r) for r in rows]


async def save_overlay(db: Executor, entry: OverlayRow) -> None:
    """Insert or replace a connection's overlay entry (validate it with conduit's
    ModelEntry first). Only that connection's entry for the model is replaced."""
    if not entry.connection_id:
        raise ValueError("an overlay entry belongs to a connection: give its connection_id")
    if entry.residency_scope not in _SCOPES:
        raise ValueError("a residency scope is local, in_region, cross_region or global")
    async with db.transaction() as conn:
        await conn.execute(
            "DELETE FROM catalogue_overlays "
            "WHERE connection_id = ? AND provider = ? AND model_id = ?",
            (entry.connection_id, entry.provider, entry.model_id),
        )
        await conn.execute(
            f"INSERT INTO catalogue_overlays ({_OVERLAY_COLUMNS}, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.provider,
                entry.model_id,
                entry.kind,
                entry.context_window,
                entry.output_limit,
                entry.embedding_dimensions,
                int(entry.tools),
                int(entry.vision),
                int(entry.prompt_caching),
                int(entry.structured_output),
                entry.residency_scope,
                json.dumps(sorted(entry.regions)),
                entry.connection_id,
                _now(),
            ),
        )
        await _notify(conn)


async def delete_overlay(
    db: Executor, provider: str, model_id: str, *, connection_id: str | None
) -> bool:
    """Remove a connection's overlay entry (``connection_id`` None: an entry of no
    connection). True if it existed."""
    scope = "connection_id = ?" if connection_id is not None else "connection_id IS NULL"
    params: tuple[str, ...] = (provider, model_id)
    if connection_id is not None:
        params += (connection_id,)
    async with db.transaction() as conn:
        removed = await conn.execute(
            f"DELETE FROM catalogue_overlays WHERE provider = ? AND model_id = ? AND {scope}",
            params,
        )
        await _notify(conn)
    return removed > 0


# -- Metering -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metering:
    """A connection's credit settings (M6 to M8). The defaults apply until one is saved."""

    connection_id: str
    metered: bool = False
    base_rate_tokens: int = 1000
    higher_multiplier: float = 2.0
    lower_multiplier: float = 0.5
    input_weight: float = 1.0
    output_weight: float = 5.0
    cache_read_weight: float = 0.1
    cache_write_weight: float = 1.25


_SAVE_METERING = upsert(
    "connection_metering",
    [
        "connection_id",
        "metered",
        "base_rate_tokens",
        "higher_multiplier",
        "lower_multiplier",
        "input_weight",
        "output_weight",
        "cache_read_weight",
        "cache_write_weight",
        "updated_at",
    ],
    ["connection_id"],
)


async def get_metering(db: Executor, connection_id: str) -> Metering:
    """A connection's metering settings (the defaults when none are saved)."""
    row = await db.fetch_one(
        "SELECT metered, base_rate_tokens, higher_multiplier, lower_multiplier, input_weight, "
        "output_weight, cache_read_weight, cache_write_weight FROM connection_metering "
        "WHERE connection_id = ?",
        (connection_id,),
    )
    if row is None:
        return Metering(connection_id)
    return Metering(
        connection_id=connection_id,
        metered=bool(row["metered"]),
        base_rate_tokens=int(row["base_rate_tokens"]),
        higher_multiplier=float(row["higher_multiplier"]),
        lower_multiplier=float(row["lower_multiplier"]),
        input_weight=float(row["input_weight"]),
        output_weight=float(row["output_weight"]),
        cache_read_weight=float(row["cache_read_weight"]),
        cache_write_weight=float(row["cache_write_weight"]),
    )


async def save_metering(db: Executor, metering: Metering) -> None:
    """Store a connection's metering settings."""
    if metering.base_rate_tokens < 1:
        raise ValueError("a base rate is at least one weighted token per credit")
    if metering.higher_multiplier <= 0 or metering.lower_multiplier <= 0:
        raise ValueError("a tier multiplier must be positive")
    weights = (
        metering.input_weight,
        metering.output_weight,
        metering.cache_read_weight,
        metering.cache_write_weight,
    )
    if any(w < 0 for w in weights):
        raise ValueError("a token weight cannot be negative")
    async with db.transaction() as conn:
        await conn.execute(
            _SAVE_METERING,
            (
                metering.connection_id,
                1 if metering.metered else 0,
                metering.base_rate_tokens,
                metering.higher_multiplier,
                metering.lower_multiplier,
                *weights,
                _now(),
            ),
        )
        await _notify(conn)


# -- Assignments ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assignment:
    """A connection assigned to a role or to one user, with its monthly credit allowance.

    Exactly one of ``role`` and ``user_guid`` is set. ``monthly_credits`` is None when
    the connection is unmetered (no limit).
    """

    connection_id: str
    role: str | None = None
    user_guid: str | None = None
    monthly_credits: int | None = None


def _assignment(row: Row) -> Assignment:
    credits = row["monthly_credits"]
    return Assignment(
        connection_id=str(row["connection_id"]),
        role=None if row["role"] is None else str(row["role"]),
        user_guid=None if row["user_guid"] is None else str(row["user_guid"]),
        monthly_credits=None if credits is None else int(credits),
    )


async def assignments(db: Executor, connection_id: str | None = None) -> list[Assignment]:
    """The assignments of one connection, or of every connection.

    Deliberately unscoped: the administrator's assignment view and the connection
    runtime's snapshot, never a user's request.
    """
    where, params = ("WHERE connection_id = ?", (connection_id,)) if connection_id else ("", ())
    rows = await db.fetch_all(
        "SELECT connection_id, role, user_guid, monthly_credits FROM connection_assignments "
        f"{where} ORDER BY connection_id, role, user_guid",
        params,
    )
    return [_assignment(r) for r in rows]


async def save_assignment(db: Executor, assignment: Assignment) -> None:
    """Assign a connection to a role or a user (replacing that pair's allowance)."""
    if (assignment.role is None) == (assignment.user_guid is None):
        raise ValueError("an assignment names a role or a user, not both and not neither")
    if assignment.monthly_credits is not None and assignment.monthly_credits < 0:
        raise ValueError("a monthly allowance cannot be negative")
    column = "role" if assignment.role is not None else "user_guid"
    target = assignment.role if assignment.role is not None else assignment.user_guid
    async with db.transaction() as conn:
        await conn.execute(
            f"DELETE FROM connection_assignments WHERE connection_id = ? AND {column} = ?",
            (assignment.connection_id, target),
        )
        await conn.execute(
            "INSERT INTO connection_assignments "
            "(connection_id, role, user_guid, monthly_credits, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                assignment.connection_id,
                assignment.role,
                assignment.user_guid,
                assignment.monthly_credits,
                _now(),
            ),
        )
        await _notify(conn)


async def delete_assignment(
    db: Executor, connection_id: str, *, role: str | None = None, user_guid: str | None = None
) -> bool:
    """Remove one role's or one user's assignment of a connection. True if it existed."""
    if (role is None) == (user_guid is None):
        raise ValueError("name a role or a user, not both and not neither")
    column, target = ("role", role) if role is not None else ("user_guid", user_guid)
    async with db.transaction() as conn:
        removed = await conn.execute(
            f"DELETE FROM connection_assignments WHERE connection_id = ? AND {column} = ?",
            (connection_id, target),
        )
        await _notify(conn)
    return removed > 0


async def connections_for(db: Executor, user_guid: str, roles: Iterable[str]) -> set[str]:
    """The ids of the connections assigned to ``user_guid`` or to any of ``roles``."""
    held: Sequence[str] = sorted(set(roles))
    marks = ", ".join("?" for _ in held)
    role_clause = f" OR role IN ({marks})" if held else ""
    rows = await db.fetch_all(
        "SELECT DISTINCT connection_id FROM connection_assignments "
        f"WHERE user_guid = ?{role_clause}",
        (user_guid, *held),
    )
    return {str(r["connection_id"]) for r in rows}
