"""The catalogue each connection runs with: conduit's default catalogue plus that
connection's own overlay entries.

conduit refuses a model its catalogue does not declare, and checks residency and
output limits against the catalogue entry, so a model SPARK uses must be declared
there. SPARK's own entries (a model pulled into Ollama, a Foundry deployment) are
overlay entries, each belonging to the connection that declared it: rows of
``catalogue_overlays`` on a server (:mod:`spark.database.connections`), the
connection's ``overlays`` in ``config.yaml`` in personal mode
(:class:`spark.connections.store.FileConnectionStore`).

:func:`connection_catalogue` builds one connection's catalogue from its own
entries only (:func:`own_entries`), so a model declared on one connection is never
offered on another, even one of the same catalogue provider. An entry of no
connection (stored before entries were scoped, and offered by no connection then)
applies to none. :func:`build_catalogue` applies the entries as one conduit
overlay named ``spark``: an entry whose key conduit already ships overrides that
entry for that connection, any other is added. An entry conduit's ``ModelEntry``
refuses is left out and logged by key, so one bad entry cannot stop the others.

:func:`offering_connections` is the rule that gave each entry stored before the
scoping its connections (the ``overlay_connection_scope`` revision keeps a frozen
copy of it; personal mode applies it when it moves such entries into
``config.yaml``).

Context windows and output limits come from this catalogue (the 0.2 resolver in
``spark.llm.context_limits`` is retired).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from conduit import Capabilities, Catalogue, CatalogueOverlay, ModelEntry, default_catalogue

from spark.database import connections as repo
from spark.db.engine import Executor

__all__ = [
    "OVERLAY_NAME",
    "Candidate",
    "build_catalogue",
    "catalogue_provider",
    "connection_catalogue",
    "entry_of",
    "load_catalogue",
    "offering_connections",
    "own_entries",
]

logger = logging.getLogger(__name__)

OVERLAY_NAME = "spark"


def entry_of(row: repo.OverlayRow) -> ModelEntry:
    """The conduit catalogue entry an overlay row declares. Raises ValueError if refused."""
    return ModelEntry.model_validate(
        {
            "provider": row.provider,
            "model_id": row.model_id,
            "kind": row.kind,
            "residency_scope": row.residency_scope,
            "regions": frozenset(row.regions),
            "capabilities": Capabilities(
                tools=row.tools,
                vision=row.vision,
                prompt_caching=row.prompt_caching,
                structured_output=row.structured_output,
            ),
            "context_window": row.context_window,
            "output_limit": row.output_limit,
            "embedding_dimensions": row.embedding_dimensions,
        }
    )


def build_catalogue(rows: Iterable[repo.OverlayRow], base: Catalogue | None = None) -> Catalogue:
    """``base`` (conduit's default catalogue) with SPARK's overlay rows applied."""
    catalogue = base if base is not None else default_catalogue()
    add: list[ModelEntry] = []
    override: list[ModelEntry] = []
    for row in rows:
        try:
            entry = entry_of(row)
        except ValueError:
            logger.warning(
                "Catalogue overlay entry %s:%s is not a valid model entry; left out",
                row.provider,
                row.model_id,
            )
            continue
        (override if catalogue.get(entry.provider, entry.model_id) else add).append(entry)
    if not add and not override:
        return catalogue
    return catalogue.apply(
        CatalogueOverlay(name=OVERLAY_NAME, add=tuple(add), override=tuple(override))
    )


def own_entries(rows: Iterable[repo.OverlayRow], connection_id: str) -> list[repo.OverlayRow]:
    """The entries among ``rows`` that belong to ``connection_id`` (never an entry of
    no connection, nor another connection's)."""
    return [row for row in rows if row.connection_id == connection_id]


def connection_catalogue(
    rows: Iterable[repo.OverlayRow], connection_id: str, base: Catalogue | None = None
) -> Catalogue:
    """``base`` (conduit's default catalogue) with ``connection_id``'s own entries applied."""
    return build_catalogue(own_entries(rows, connection_id), base)


async def load_catalogue(
    db: Executor, connection_id: str, base: Catalogue | None = None
) -> Catalogue:
    """A server connection's catalogue: ``base`` plus its stored overlay entries."""
    return connection_catalogue(await repo.list_overlays(db, connection_id), connection_id, base)


def catalogue_provider(settings: Mapping[str, Any]) -> str:
    """The catalogue provider of a connection's settings without validating them
    (conduit's rule: the kind; for an OpenAI-compatible connection its service, or
    for a Foundry service the catalogue it names, ``openai`` by default)."""
    kind = str(settings.get("kind") or "")
    if kind != "openai_compat":
        return kind
    service = settings.get("service")
    if not isinstance(service, Mapping):
        return kind
    name = service.get("name")
    if name == "foundry":
        return str(service.get("catalogue_provider") or "openai")
    return str(name) if name else kind


@dataclass(frozen=True, slots=True)
class Candidate:
    """A connection an entry stored before the scoping may belong to: its id, catalogue
    provider and stored model choices (None when it has none: it is uncurated)."""

    connection_id: str
    catalogue_provider: str
    models: Mapping[str, bool] | None = None


def offering_connections(entry: repo.OverlayRow, candidates: Iterable[Candidate]) -> list[str]:
    """The connections that offered ``entry`` while entries applied to every connection
    of their catalogue provider, by id: those of its provider that, for a chat entry,
    are uncurated or have the model enabled. Residency is left to run time."""
    found: list[str] = []
    for candidate in candidates:
        if candidate.catalogue_provider != entry.provider:
            continue
        if entry.kind == "chat" and candidate.models is not None:
            if not candidate.models.get(entry.model_id, False):
                continue
        found.append(candidate.connection_id)
    return sorted(found)
