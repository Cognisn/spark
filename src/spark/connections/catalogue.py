"""The catalogue SPARK runs with: conduit's default catalogue plus SPARK's overlay rows.

conduit refuses a model its catalogue does not declare, and checks residency and
output limits against the catalogue entry, so a model SPARK uses must be declared
there. SPARK's own entries (a model pulled into Ollama, a Foundry deployment, a
correction of a shipped entry) are rows of ``catalogue_overlays``
(:mod:`spark.database.connections`). :func:`build_catalogue` applies them as one
conduit overlay named ``spark``: a row whose key conduit already ships overrides
that entry, any other row is added. A row conduit's ``ModelEntry`` refuses is left
out and logged by key, so one bad row cannot stop the others.

Context windows and output limits come from this catalogue (the 0.2 resolver in
``spark.llm.context_limits`` is retired).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from conduit import Capabilities, Catalogue, CatalogueOverlay, ModelEntry, default_catalogue

from spark.database import connections as repo
from spark.db.engine import Executor

__all__ = ["OVERLAY_NAME", "build_catalogue", "entry_of", "load_catalogue"]

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


async def load_catalogue(db: Executor, base: Catalogue | None = None) -> Catalogue:
    """The runtime catalogue: ``base`` (conduit's default) plus the stored overlay rows."""
    return build_catalogue(await repo.list_overlays(db), base)
