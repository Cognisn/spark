"""Which models a connection can reach, and the models view built on it (M5).

:func:`discover` asks conduit what a connection's provider lists
(``ProviderRegistry.discover``, conduit 0.1.0), through the process's conduit
loop: it blocks, so callers run it on a worker thread (``asyncio.to_thread``),
never on conduit's own loop. conduit validates the settings, resolves the
secrets, builds the client, lists, and closes the client again; SPARK makes no
provider call of its own. Each listed model comes back allowed or refused (with
conduit's reason), with where the provider processes it, whether the catalogue declares
it, and the drift between the catalogue and the listing (shown as information). A failed listing is recorded with conduit's
failure class only, never provider text.

The latest result per connection is kept in this process (:data:`RESULTS`), with
the time it ran. Nothing here makes a model usable: a model is offered only
through the connection's catalogue (with its own overlay entries) and its model rows.

:func:`models_view` is what the connections screens show for one connection:
every chat model the connection's catalogue (conduit's, with the connection's own
overlay entries) declares for its catalogue provider and offers in its Region, each with whether the
last discovery reached it, conduit's reason where the residency policy refuses
it, whether it is enabled and its tier; and, apart, the discovered models the
catalogue does not declare (Ollama's always arrive this way), which an
administrator may declare as overlay rows.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from conduit import (
    Catalogue,
    ConduitError,
    DiscoveryOutcome,
    DiscoveryResult,
    DriftReason,
    LoopThread,
    ModelEntry,
    ProviderError,
    ProviderFailure,
    ProviderRegistry,
    ProviderSettings,
    ResidencyPolicy,
    ResidencyRefused,
    ResidencyScope,
)

from spark.connections.model import Connection, ConnectionModel

__all__ = [
    "RESULTS",
    "DiscoveryHost",
    "DRIFT_TEXT",
    "DiscoveredRow",
    "DiscoveryRecord",
    "DiscoveryResults",
    "DriftNote",
    "Undeclared",
    "discover",
    "models_view",
    "offered_in",
    "record_of",
    "refusal",
]

#: Plain words for each failure class conduit reports (never the provider's own text).
_FAILURES: Final[Mapping[ProviderFailure, str]] = {
    ProviderFailure.UNAVAILABLE: "the service is unavailable (it may be rate limited or down)",
    ProviderFailure.TIMEOUT: "the service timed out",
    ProviderFailure.REJECTED: "the service refused the request (check the credentials)",
    ProviderFailure.REFUSED: "the service refused to list its models",
    ProviderFailure.PROTOCOL: "the service's answer could not be read",
}


class DiscoveryHost(Protocol):
    """What :func:`discover` needs of the connection runtime."""

    @property
    def loop(self) -> LoopThread: ...

    @property
    def timeout(self) -> float: ...

    def registry(
        self, residency: Any = None, *, connection_id: str | None = None
    ) -> ProviderRegistry: ...


@dataclass(frozen=True, slots=True)
class Undeclared:
    """A model the service lists that the catalogue does not declare."""

    model_id: str
    label: str | None = None


#: Plain words for each kind of drift conduit reports. The drift never hides a model.
DRIFT_TEXT: Final[Mapping[str, str]] = {
    DriftReason.UNCATALOGUED.value: (
        "conduit has no capability record for this model; using defaults"
    ),
    DriftReason.UNLISTED.value: "the catalogue declares this model, but the service does not list it",
    DriftReason.LIMITS_DIFFER.value: "the service reports different limits from the catalogue",
    DriftReason.CAPABILITIES_DIFFER.value: (
        "the service reports different capabilities from the catalogue"
    ),
    DriftReason.REGIONS_DECLARED.value: (
        "this connection declares where the model is processed, which differs from the listing"
    ),
}


@dataclass(frozen=True, slots=True)
class DiscoveredRow:
    """One model the service lists, joined to the catalogue and the connection's residency.

    ``allowed`` is whether conduit allows it under the connection's residency;
    ``reason`` is conduit's reason when it does not. ``scope`` and ``regions`` are where
    the provider says it is processed.
    """

    model_id: str
    label: str | None = None
    allowed: bool = True
    reason: str | None = None
    scope: str = "global"
    regions: frozenset[str] = frozenset()
    catalogued: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "label": self.label,
            "allowed": self.allowed,
            "reason": self.reason,
            "scope": self.scope,
            "regions": sorted(self.regions),
            "catalogued": self.catalogued,
        }


@dataclass(frozen=True, slots=True)
class DriftNote:
    """One difference between the catalogue and the listing (conduit's ``DriftReason``)."""

    model_id: str
    reason: str
    detail: str

    @property
    def text(self) -> str:
        return DRIFT_TEXT.get(self.reason, self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "reason": self.reason,
            "text": self.text,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    """The latest discovery on one connection.

    ``supported`` is False when conduit has no listing for the connection's
    settings (``reason`` says why); ``error`` is set when the listing failed.
    """

    connection_id: str
    ran_at: str
    supported: bool = True
    reason: str | None = None
    error: str | None = None
    #: Every model id the service listed, declared or not.
    reachable: frozenset[str] = frozenset()
    #: Each listed model, joined to the catalogue and the connection's residency.
    models: tuple[DiscoveredRow, ...] = ()
    #: The drift between the catalogue and the listing, as information.
    drift: tuple[DriftNote, ...] = ()
    #: The connection's Region choice the listing was held to (regions, allow_global), or None.
    choice: Mapping[str, Any] | None = None

    @property
    def undeclared(self) -> tuple[Undeclared, ...]:
        """The listed models no catalogue entry matched (conduit's ``uncatalogued``)."""
        return tuple(Undeclared(m.model_id, m.label) for m in self.models if not m.catalogued)

    def row(self, model_id: str) -> DiscoveredRow | None:
        for row in self.models:
            if row.model_id == model_id:
                return row
        return None

    @property
    def ok(self) -> bool:
        return self.supported and self.error is None

    def summary(self) -> str:
        """One line for the connections list."""
        if self.error is not None:
            return f"Failed: {self.error}"
        if not self.supported:
            return f"Not supported: {self.reason or 'no model listing'}"
        found = len(self.reachable - {u.model_id for u in self.undeclared})
        text = f"{found} model{'s' if found != 1 else ''} reachable"
        if self.undeclared:
            text += f", {len(self.undeclared)} not in the catalogue"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "ok": self.ok,
            "supported": self.supported,
            "reason": self.reason,
            "error": self.error,
            "summary": self.summary(),
            "reachable": sorted(self.reachable),
            "undeclared": [{"model_id": u.model_id, "label": u.label} for u in self.undeclared],
            "models": [m.as_dict() for m in self.models],
            "drift": [d.as_dict() for d in self.drift],
            "choice": dict(self.choice) if self.choice is not None else None,
        }


class DiscoveryResults:
    """The latest discovery per connection, in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, DiscoveryRecord] = {}

    def get(self, connection_id: str) -> DiscoveryRecord | None:
        with self._lock:
            return self._records.get(connection_id)

    def put(self, record: DiscoveryRecord) -> None:
        with self._lock:
            self._records[record.connection_id] = record

    def forget(self, connection_id: str) -> None:
        with self._lock:
            self._records.pop(connection_id, None)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


#: The process's discovery results.
RESULTS: Final = DiscoveryResults()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _record(connection_id: str, result: DiscoveryResult) -> DiscoveryRecord:
    if not result.supported:
        return DiscoveryRecord(connection_id, _now(), supported=False, reason=result.reason)
    return record_of(connection_id, result)


def record_of(connection_id: str, result: DiscoveryResult) -> DiscoveryRecord:
    """The record of a supported conduit discovery result."""
    rows = tuple(
        DiscoveredRow(
            m.model_id,
            m.label,
            allowed=m.outcome is DiscoveryOutcome.ALLOWED,
            reason=m.reason,
            scope=m.scope.value,
            regions=m.regions,
            catalogued=m.catalogued,
        )
        for m in result.models
    )
    drift = tuple(DriftNote(d.model_id, d.reason.value, d.detail) for d in result.drift)
    choice = (
        {"regions": sorted(result.choice.regions), "allow_global": result.choice.allow_global}
        if result.choice is not None
        else None
    )
    return DiscoveryRecord(
        connection_id,
        _now(),
        reachable=frozenset(m.model_id for m in rows),
        models=rows,
        drift=drift,
        choice=choice,
    )


def discover(
    host: DiscoveryHost, connection: Connection, results: DiscoveryResults | None = RESULTS
) -> DiscoveryRecord:
    """Ask conduit what ``connection`` lists, record it and return it.

    Blocks on the conduit loop: call it from a worker thread. A failure is
    recorded (conduit's failure class or refusal, never provider text), not raised.
    """
    # The connection's own catalogue: a model declared on another connection is undeclared here.
    registry = host.registry(connection.residency, connection_id=connection.id)
    record: DiscoveryRecord
    try:
        result = host.loop.run(
            registry.discover(connection.id, dict(connection.settings)), timeout=host.timeout
        )
    except ProviderError as failed:
        reason = _FAILURES.get(failed.failure, f"the listing failed ({failed.failure.value})")
        record = DiscoveryRecord(connection.id, _now(), error=reason)
    except ConduitError as refused:
        # conduit's messages name fields and kinds, never values.
        record = DiscoveryRecord(connection.id, _now(), error=str(refused))
    else:
        record = _record(connection.id, result)
    if results is not None:
        results.put(record)
    return record


def offered_in(entry: ModelEntry, region: str | None) -> bool:
    """Whether the catalogue offers ``entry`` in ``region`` (conduit's start-up rule: an
    in-Region or cross-Region model lists its Regions; any other is offered anywhere)."""
    if entry.residency_scope not in (ResidencyScope.IN_REGION, ResidencyScope.CROSS_REGION):
        return True
    return region is None or region in entry.regions


def refusal(
    policy: ResidencyPolicy,
    entry: ModelEntry,
    region: str | None,
    settings: ProviderSettings | None,
    task: str,
) -> str | None:
    """Why ``policy`` (and the Region choice in ``settings``, the connection's validated
    provider settings) refuses ``entry`` called from ``region``, or None (conduit 0.1.0
    holds a model to both)."""
    try:
        policy.check(entry, region, task=task, provider=settings)
    except ResidencyRefused as refused:
        return str(refused)
    return None


def _scope_of(entry: ModelEntry) -> str:
    """Where the catalogue says ``entry`` is processed (conduit 0.1.0's default catalogue
    states none for most models: unbounded, so ``global``, until a listing says otherwise)."""
    return (entry.residency_scope or ResidencyScope.GLOBAL).value


@dataclass(frozen=True, slots=True)
class _Row:
    entry: ModelEntry
    overlay: bool
    enabled: bool
    tier: str
    refused: str | None
    reachable: bool | None
    #: Where the provider says it processes the model (None: no discovery lists it).
    scope: str | None = None
    regions: frozenset[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        caps = self.entry.capabilities
        return {
            "model_id": self.entry.model_id,
            "context_window": self.entry.context_window,
            "output_limit": self.entry.output_limit,
            "tools": caps.tools,
            "vision": caps.vision,
            "prompt_caching": caps.prompt_caching,
            "structured_output": caps.structured_output,
            "residency_scope": self.scope if self.scope is not None else _scope_of(self.entry),
            "regions": sorted(self.regions if self.regions is not None else self.entry.regions),
            "overlay": self.overlay,
            "enabled": self.enabled,
            "tier": self.tier,
            "refused": self.refused,
            "reachable": self.reachable,
        }


@dataclass(frozen=True, slots=True)
class _View:
    rows: list[_Row] = field(default_factory=list)
    undeclared: list[Undeclared] = field(default_factory=list)


def models_view(
    connection: Connection,
    catalogue: Catalogue,
    catalogue_provider: str,
    stored: Iterable[ConnectionModel] | None,
    record: DiscoveryRecord | None,
    overlay_keys: Collection[tuple[str, str]] = (),
    settings: ProviderSettings | None = None,
) -> dict[str, Any]:
    """The models view of one connection (see the module docstring).

    ``stored`` is the connection's model rows, or None when none are stored (every
    catalogue model is then enabled at the standard tier). ``catalogue`` is the
    connection's own; ``overlay_keys`` are the (provider, model id) pairs of the
    overlay entries the connection declared. ``settings`` are the connection's validated
    provider settings: conduit holds each model to the connection's Region choice in them,
    and the Region they name (set by a choice such as an OpenAI data residency) is the
    one the connection is called in.

    Where the last discovery lists a model, the row shows where the provider processes
    it (its scope and Regions) and conduit's verdict on it under the connection's
    residency; a model the discovery lists is never left out for the catalogue's Regions.
    ``drift`` carries the differences between the catalogue and the listing, as information.
    """
    region = settings.region if settings is not None and settings.region else connection.region
    listed = record if record is not None and record.ok else None
    rows_by_id = {m.model_id: m for m in stored} if stored is not None else None
    view = _View()
    declared: set[str] = set()
    for entry in sorted(catalogue, key=lambda e: e.model_id):
        if entry.provider != catalogue_provider or entry.kind != "chat":
            continue
        found = listed.row(entry.model_id) if listed is not None else None
        if found is None and not offered_in(entry, region):
            continue
        declared.add(entry.model_id)
        row = rows_by_id.get(entry.model_id) if rows_by_id is not None else None
        enabled = rows_by_id is None or (row is not None and row.enabled)
        reachable: bool | None = None
        if listed is not None:
            reachable = found is not None
        view.rows.append(
            _Row(
                entry=entry,
                overlay=(catalogue_provider, entry.model_id) in overlay_keys,
                enabled=enabled,
                tier=row.tier if row is not None else "standard",
                refused=(
                    found.reason
                    if found is not None
                    else refusal(connection.residency, entry, region, settings, connection.id)
                ),
                reachable=reachable,
                scope=found.scope if found is not None else None,
                regions=found.regions if found is not None else None,
            )
        )
    if record is not None and record.ok:
        view.undeclared.extend(u for u in record.undeclared if u.model_id not in declared)
    models = [r.as_dict() for r in view.rows]
    shown = {m["model_id"] for m in models}
    for row in sorted(stored or (), key=lambda r: r.model_id):
        if row.model_id not in shown:
            models.append(_absent(row, listed))
    return {
        "catalogue_provider": catalogue_provider,
        "curated": stored is not None,
        "models": models,
        "undeclared": [{"model_id": u.model_id, "label": u.label} for u in view.undeclared],
        "discovery": record.as_dict() if record is not None else None,
        "discovered": _discovered(listed),
        "drift": [d.as_dict() for d in listed.drift] if listed is not None else [],
    }


def _absent(row: ConnectionModel, listed: DiscoveryRecord | None) -> dict[str, Any]:
    """A stored choice for a model this connection does not serve now (for example one the
    service no longer lists, or one a deployment named before discovery ran): shown with the
    reason, kept as it is, never offered. It returns with the model."""
    if listed is not None and row.model_id in listed.reachable:
        reason = "the catalogue has no entry for this model, so it cannot be offered yet"
    elif listed is not None:
        reason = "the service does not list this model"
    else:
        reason = "this connection does not serve this model now (run discovery to check)"
    return {
        "model_id": row.model_id,
        "context_window": None,
        "output_limit": None,
        "tools": None,
        "vision": None,
        "prompt_caching": None,
        "structured_output": None,
        "residency_scope": None,
        "regions": [],
        "overlay": False,
        "enabled": row.enabled,
        "tier": row.tier,
        "refused": reason,
        "reachable": False if listed is not None else None,
        "absent": True,
    }


def _discovered(listed: DiscoveryRecord | None) -> list[dict[str, Any]]:
    """Every model the service lists with where it is processed, whether conduit allows it
    under the connection's residency and why not, and the drift noted for it."""
    if listed is None:
        return []
    notes: dict[str, list[str]] = {}
    for d in listed.drift:
        notes.setdefault(d.model_id, []).append(d.reason)
    return [{**m.as_dict(), "drift": notes.get(m.model_id, [])} for m in listed.models]
