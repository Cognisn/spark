"""The Amazon Bedrock connection ``spark bootstrap`` adds (server mode).

On AWS a deployment calls Bedrock through the task's IAM role: a Bedrock connection
whose authentication is conduit's ``credential_chain`` holds no key and no
``secret://`` reference, because boto3's default chain finds the role's credentials.
When ``bootstrap.bedrock.region`` is set, bootstrap adds that connection so a fresh
deployment can answer without an administrator adding one first.

The rule, once only and never against an administrator:

* the connection is ``aws-bedrock`` (the id the 0.2 upgrade also uses);
* if a connection with that id exists, in any state, nothing changes;
* bootstrap records that it has dealt with the id (an instance setting row,
  :data:`MARKER`), whether it created the connection or found one. A connection that
  is missing while that record exists was deleted by an administrator, so it is not
  added again;
* an instance whose bootstrap ran before this setting existed has no record and no
  connection, so the next run adds it.

The connection is built and validated by :mod:`spark.connections.model`, saved through
:class:`spark.connections.store.DatabaseConnectionStore`, and audited as the Connections
page audits a created connection (names and kinds only), attributed to bootstrap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from conduit import ProviderRegistry, SecretRef
from conduit.core.residency import ResidencyPolicy, ResidencyScope
from pydantic import SecretStr

from spark.bootstrap.settings import BedrockSeed
from spark.connections.model import (
    Connection,
    default_residency,
    validate,
)
from spark.connections.runtime import production_factories
from spark.connections.store import DatabaseConnectionStore
from spark.db.dialect import upsert
from spark.db.engine import Database

__all__ = ["CONNECTION_ID", "MARKER", "build", "seed_bedrock"]

#: The id of the connection bootstrap adds.
CONNECTION_ID: Final = "aws-bedrock"
#: The instance settings row that records bootstrap has dealt with :data:`CONNECTION_ID`.
#: It is not a registered setting: only bootstrap reads and writes it.
MARKER: Final = "bootstrap.bedrock.seeded"

#: ``created`` (added now), ``exists`` (one was there, left alone), ``declined`` (an
#: administrator deleted it earlier, not added again).
Result = Literal["created", "exists", "declined"]

_MARKER_UPSERT = upsert("instance_settings", ["key", "value", "updated_at", "updated_by"], ["key"])


class _NoSecrets:
    """The connection holds no secret reference; validation never resolves one."""

    async def resolve(self, ref: SecretRef) -> SecretStr:
        raise LookupError("a bootstrapped Bedrock connection holds no secret reference")


def build(seed: BedrockSeed) -> Connection:
    """The connection ``seed`` describes: task role credentials, enabled."""
    settings: dict[str, Any] = {
        "kind": "bedrock",
        "region": seed.region,
        "auth": {"mode": "credential_chain"},
    }
    if seed.residency_regions is None:
        residency = default_residency(settings)
    else:
        residency = ResidencyPolicy(
            scopes=frozenset(
                {ResidencyScope.LOCAL, ResidencyScope.IN_REGION, ResidencyScope.CROSS_REGION}
            ),
            regions=frozenset(seed.residency_regions),
        )
    return Connection(CONNECTION_ID, seed.name, settings, residency, True)


def _fields(settings: Mapping[str, Any], path: str = "") -> list[str]:
    names: list[str] = []
    for key, value in settings.items():
        dotted = f"{path}.{key}" if path else str(key)
        names.extend(_fields(value, dotted) if isinstance(value, Mapping) else [dotted])
    return names


async def _mark(database: Database, actor: str) -> None:
    await database.execute(
        _MARKER_UPSERT, (MARKER, json.dumps(True), datetime.now(UTC).isoformat(), actor)
    )


async def seed_bedrock(database: Database, seed: BedrockSeed, *, actor: str) -> Result:
    """Add the Bedrock connection unless it exists or an administrator removed it.

    Raises :class:`spark.connections.model.ConnectionInvalid` if conduit refuses the
    settings (nothing is written then).
    """
    from spark.identity import audit

    store = DatabaseConnectionStore(database)
    marked = await database.fetch_value("SELECT 1 FROM instance_settings WHERE key = ?", (MARKER,))
    if await store.get_connection(CONNECTION_ID) is not None:
        if marked is None:
            await _mark(database, actor)
        return "exists"
    if marked is not None:
        return "declined"
    connection = build(seed)
    registry = ProviderRegistry(secrets=_NoSecrets(), factories=production_factories())
    validate(connection, registry)
    await store.save_connection(connection)
    await _mark(database, actor)
    await audit.emit(
        audit.SETTING_CHANGED,
        actor=actor,
        resource_type="connection",
        resource_id=connection.id,
        details={
            "connection_id": connection.id,
            "change": "created",
            "fields": sorted([*_fields(connection.settings), "name", "residency"]),
        },
    )
    return "created"
