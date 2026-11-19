"""Which models a connection can reach, and the models view built on it (M5).

:func:`discover` asks conduit what a connection's provider lists
(``ProviderRegistry.discover``, conduit 0.1.0rc3), through the process's conduit
loop: it blocks, so callers run it on a worker thread (``asyncio.to_thread``),
never on conduit's own loop. conduit validates the settings, resolves the
secrets, builds the client, lists, and closes the client again; SPARK makes no
provider call of its own. Each listed model comes back allowed, refused (with
conduit's reason) or undeclared. A failed listing is recorded with conduit's
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
    DiscoveryResult,
    LoopThread,
    ModelEntry,
    ProviderError,
    ProviderFailure,
    ProviderRegistry,
    ResidencyPolicy,
    ResidencyRefused,
    ResidencyScope,
)

from spark.connections.model import Connection, ConnectionModel

__all__ = [
    "RESULTS",
    "DiscoveryHost",
    "DiscoveryRecord",
    "DiscoveryResults",
    "Undeclared",
    "discover",
    "models_view",
    "offered_in",
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
    undeclared: tuple[Undeclared, ...] = ()

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
    reachable = frozenset(m.model_id for m in result.models)
    undeclared = tuple(Undeclared(m.model_id, m.label) for m in result.undeclared)
    return DiscoveryRecord(connection_id, _now(), reachable=reachable, undeclared=undeclared)


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


def _refusal(policy: ResidencyPolicy, entry: ModelEntry, connection: Connection) -> str | None:
    try:
        policy.check(entry, connection.region, task=connection.id)
    except ResidencyRefused as refused:
        return str(refused)
    return None


@dataclass(frozen=True, slots=True)
class _Row:
    entry: ModelEntry
    overlay: bool
    enabled: bool
    tier: str
    refused: str | None
    reachable: bool | None

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
            "residency_scope": self.entry.residency_scope.value,
            "regions": sorted(self.entry.regions),
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
) -> dict[str, Any]:
    """The models view of one connection (see the module docstring).

    ``stored`` is the connection's model rows, or None when none are stored (every
    catalogue model is then enabled at the standard tier). ``catalogue`` is the
    connection's own; ``overlay_keys`` are the (provider, model id) pairs of the
    overlay entries the connection declared.
    """
    rows_by_id = {m.model_id: m for m in stored} if stored is not None else None
    view = _View()
    declared: set[str] = set()
    for entry in sorted(catalogue, key=lambda e: e.model_id):
        if entry.provider != catalogue_provider or entry.kind != "chat":
            continue
        if not offered_in(entry, connection.region):
            continue
        declared.add(entry.model_id)
        row = rows_by_id.get(entry.model_id) if rows_by_id is not None else None
        enabled = rows_by_id is None or (row is not None and row.enabled)
        reachable: bool | None = None
        if record is not None and record.ok:
            reachable = entry.model_id in record.reachable
        view.rows.append(
            _Row(
                entry=entry,
                overlay=(catalogue_provider, entry.model_id) in overlay_keys,
                enabled=enabled,
                tier=row.tier if row is not None else "standard",
                refused=_refusal(connection.residency, entry, connection),
                reachable=reachable,
            )
        )
    if record is not None and record.ok:
        view.undeclared.extend(u for u in record.undeclared if u.model_id not in declared)
    return {
        "catalogue_provider": catalogue_provider,
        "curated": stored is not None,
        "models": [r.as_dict() for r in view.rows],
        "undeclared": [{"model_id": u.model_id, "label": u.label} for u in view.undeclared],
        "discovery": record.as_dict() if record is not None else None,
    }
