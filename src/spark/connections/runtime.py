"""The connection runtime: one conduit provider per enabled connection, kept current.

One :class:`ConnectionRuntime` serves a process (the web process, the worker, the
personal tray daemon). It owns the process's conduit loop
(:class:`conduit.LoopThread`), started once; SPARK's secret resolver over konfig
(server: the shared store; personal: the OS keychain, A9); and SPARK's usage sink.

:meth:`ConnectionRuntime.apply` takes a :class:`Snapshot` (the connections,
their models and assignments, and SPARK's catalogue overlay entries, as stored) and
builds a conduit provider for each enabled connection through conduit's provider
registry, the single place a provider client is made. Each connection has a
registry of its own holding the connection's residency policy and the connection's
own catalogue (conduit's catalogue plus the overlay entries that connection
declared, :func:`~spark.connections.catalogue.connection_catalogue`), so conduit
checks every call against them and a model declared on one connection is never
offered or served on another. Several connections of one kind coexist.

Applying a later snapshot rebuilds only the connections whose settings, residency,
resolved secrets or own overlay entries changed (server mode: the settings
follower calls :meth:`refresh` after every LISTEN/NOTIFY reload). conduit's
provider keeps the catalogue it was built with, so a model declared on a running
connection, declared again or removed rebuilds that connection's provider. A
rebuilt provider replaces the old one only once it has been built; a connection
that fails to build keeps its previous provider, so a bad change never drops a
working connection (D38). A replaced provider is closed after a grace period, so
calls already on it finish.

The models a connection offers are its enabled ``connection_models`` rows, or,
before any are stored for it, every chat model its catalogue (conduit's, with the
connection's own overlay entries) declares for its catalogue provider; either way only those its residency policy allows.
A chosen model is a :class:`~spark.connections.refs.ModelRef`.

On a server a user may use a connection assigned to them or to one of their
roles; a connection with no assignment at all is visible to administrators only,
so it can be tried before it is assigned (M9). Personal mode's single user may use
every connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from conduit import (
    Catalogue,
    ConduitError,
    LoopThread,
    MeteredProvider,
    ModelEntry,
    ProviderFactory,
    ProviderRegistry,
    ProviderSettings,
    ResidencyRefused,
    SecretRef,
    SecretResolver,
)
from conduit.providers import builtin_factories

from spark.connections import model as model_mod
from spark.connections.catalogue import build_catalogue, connection_catalogue, own_entries
from spark.connections.model import Connection, ConnectionInvalid, ConnectionModel
from spark.connections.refs import ModelRef
from spark.connections.service import ConnectionService, ModelUnavailable
from spark.connections.usage import Attribution, UsageSink
from spark.database import connections as repo

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CatalogueLimits",
    "ConnectionRuntime",
    "ModelSource",
    "Snapshot",
    "UserScope",
    "production_factories",
    "snapshot_of",
]

logger = logging.getLogger(__name__)

#: Per-step timeout of a model call through the facade.
DEFAULT_TIMEOUT_S: Final = 600.0
#: How long a replaced provider stays open for calls already on it.
DRAIN_S: Final = 600.0
#: The (context window, output limit) used for a model the catalogue does not declare.
FALLBACK_LIMITS: Final = (8_192, 4_096)


def production_factories() -> list[ProviderFactory]:
    """Every conduit adapter M4 offers: conduit's built-in ones, without the test stub."""
    return [f for f in builtin_factories() if f.kind != "stub"]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The connection configuration as stored, read in one go."""

    connections: tuple[Connection, ...] = ()
    #: Per connection, its stored model rows; a connection absent here has none stored.
    models: Mapping[str, tuple[ConnectionModel, ...]] = field(default_factory=dict)
    #: Per connection, its assignments (server mode).
    assignments: Mapping[str, tuple[repo.Assignment, ...]] = field(default_factory=dict)
    #: SPARK's overlay entries, each applied to its own connection's catalogue only.
    overlays: tuple[repo.OverlayRow, ...] = ()


@dataclass(frozen=True, slots=True)
class UserScope:
    """Who is choosing a model, on a server: their guid, roles and whether they administer."""

    user_guid: str
    roles: frozenset[str] = frozenset()
    administrator: bool = False


class ModelSource(Protocol):
    """What SPARK's orchestrators ask of the connections (the runtime implements it).

    ``service`` gives a service of its own for a chosen model (``connection/model``,
    or a bare model id as stored before connections), or None when no connection
    the process runs has it.
    """

    def service(
        self, model: str | ModelRef, *, attribution: Attribution | None = None
    ) -> Any | None: ...

    def resolve(self, model: str, user: UserScope | None = None) -> ModelRef | None: ...

    def limits(self, model: str) -> tuple[int, int]: ...

    def listed_models(self, connection_id: str) -> list[dict[str, Any]]: ...

    def may_use(self, connection_id: str, user: UserScope | None) -> bool: ...

    def connection_name(self, connection_id: str) -> str: ...


@dataclass(slots=True)
class _Built:
    connection: Connection
    settings: ProviderSettings
    provider: MeteredProvider
    fingerprint: str


def _secret_refs(value: Any) -> list[SecretRef]:
    refs: list[SecretRef] = []
    if isinstance(value, Mapping):
        for item in value.values():
            refs.extend(_secret_refs(item))
    elif isinstance(value, list | tuple):
        for item in value:
            refs.extend(_secret_refs(item))
    elif isinstance(value, str) and value.startswith("secret://"):
        try:
            refs.append(SecretRef.parse(value))
        except ValueError:
            pass
    return refs


def _overlay_digest(rows: Iterable[repo.OverlayRow], connection_id: str) -> str:
    """A digest of ``connection_id``'s own overlay entries, every field, in a fixed order.

    conduit's provider keeps the catalogue it was built with and checks each call
    against it, so this digest is part of a provider's fingerprint: declaring a model
    on a connection, declaring it again or removing it rebuilds that connection's
    provider, and no other connection's (wave 10 Q54).
    """
    canonical = sorted(
        repr(
            (
                row.provider,
                row.model_id,
                row.kind,
                row.context_window,
                row.output_limit,
                row.embedding_dimensions,
                row.tools,
                row.vision,
                row.prompt_caching,
                row.structured_output,
                row.residency_scope,
                tuple(sorted(row.regions)),
            )
        )
        for row in own_entries(rows, connection_id)
    )
    return hashlib.sha256("\n".join(canonical).encode()).hexdigest()


class ConnectionRuntime:
    """The process's conduit loop, connections and their providers."""

    def __init__(
        self,
        *,
        secrets: SecretResolver,
        usage: UsageSink | None = None,
        factories: Sequence[ProviderFactory] | None = None,
        catalogue: Catalogue | None = None,
        loop: LoopThread | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        drain_s: float = DRAIN_S,
    ) -> None:
        self._secrets = secrets
        self._usage = usage
        self._factories = list(factories) if factories is not None else production_factories()
        self._catalogue = build_catalogue((), catalogue)
        self._catalogues: dict[str, Catalogue] = {}
        self._owns_loop = loop is None
        self._loop = loop if loop is not None else LoopThread(name="spark-conduit")
        self._timeout = timeout
        self._drain_s = drain_s
        self._lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self._built: dict[str, _Built] = {}
        self._connections: dict[str, Connection] = {}
        self._models: dict[str, tuple[ConnectionModel, ...]] = {}
        self._assignments: dict[str, tuple[repo.Assignment, ...]] = {}
        self._failures: dict[str, str] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- what the service and callers read ----------------------------------------------

    @property
    def loop(self) -> LoopThread:
        return self._loop

    @property
    def usage(self) -> UsageSink | None:
        return self._usage

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def catalogue(self) -> Catalogue:
        """The catalogue conduit ships (the runtime's base), without any overlay entry."""
        return self._catalogue

    def catalogue_for(self, connection_id: str) -> Catalogue:
        """A configured connection's catalogue: conduit's, with that connection's own
        overlay entries (conduit's alone for a connection this runtime does not know)."""
        with self._lock:
            return self._catalogues.get(connection_id, self._catalogue)

    def registry(
        self, residency: Any = None, *, connection_id: str | None = None
    ) -> ProviderRegistry:
        """A conduit provider registry with this runtime's adapters, secrets and sink, and
        ``connection_id``'s catalogue (conduit's alone when None): validation only,
        unless a provider is built through it."""
        return ProviderRegistry(
            secrets=self._secrets,
            catalogue=(
                self.catalogue_for(connection_id) if connection_id is not None else self.catalogue
            ),
            events=self._usage,
            residency=residency,
            factories=self._factories,
        )

    def validate(self, connection: Connection) -> ProviderSettings:
        """``connection``'s settings, validated by conduit's registry (raises
        :class:`ConnectionInvalid`)."""
        return model_mod.validate(connection, self.registry())

    def connections(self) -> list[Connection]:
        """Every configured connection (enabled or not), by id."""
        with self._lock:
            return [self._connections[c] for c in sorted(self._connections)]

    def connection(self, connection_id: str) -> Connection | None:
        with self._lock:
            return self._connections.get(connection_id)

    def connection_name(self, connection_id: str) -> str:
        connection = self.connection(connection_id)
        return connection.name if connection is not None else connection_id

    def access_info(self, connection_id: str) -> str:
        connection = self.connection(connection_id)
        if connection is None:
            return connection_id
        service = model_mod.SERVICES.get(connection.service or "")
        label = service.label if service is not None else connection.kind
        return f"{label} ({connection.region})" if connection.region else label

    def failures(self) -> dict[str, str]:
        """Connections that could not be built, with conduit's reason (class names only)."""
        with self._lock:
            return dict(self._failures)

    def provider(self, connection_id: str) -> MeteredProvider:
        """The connection's built provider. Raises :class:`ModelUnavailable`."""
        with self._lock:
            built = self._built.get(connection_id)
            failure = self._failures.get(connection_id)
        if built is None:
            reason = f": {failure}" if failure else ""
            raise ModelUnavailable(f"connection {connection_id!r} is not available{reason}")
        return built.provider

    def _catalogue_provider(self, connection_id: str) -> str | None:
        with self._lock:
            built = self._built.get(connection_id)
        return built.settings.catalogue_provider if built is not None else None

    def entry(self, ref: ModelRef) -> ModelEntry:
        """The catalogue entry for a model on a connection, within the connection's policy.

        Raises conduit's ``UndeclaredModel`` or ``ResidencyRefused``, or
        :class:`ModelUnavailable` for a connection that is not running.
        """
        connection = self.connection(ref.connection_id)
        provider = self._catalogue_provider(ref.connection_id)
        if connection is None or provider is None:
            raise ModelUnavailable(f"connection {ref.connection_id!r} is not available")
        entry = self.catalogue_for(ref.connection_id).require(provider, ref.model_id)
        connection.residency.check(entry, connection.region, task=ref.connection_id)
        return entry

    def refusal(self, ref: ModelRef) -> str | None:
        """Why ``ref`` cannot be used (conduit's reason), or None when it can."""
        try:
            self.entry(ref)
        except (ConduitError, ModelUnavailable) as refused:
            return str(refused)
        return None

    def _offered(self, connection_id: str) -> list[tuple[ModelEntry, model_mod.Tier]]:
        """The chat models a running connection offers, with their tiers, by model id."""
        connection = self.connection(connection_id)
        provider = self._catalogue_provider(connection_id)
        if connection is None or provider is None:
            return []
        catalogue = self.catalogue_for(connection_id)
        with self._lock:
            stored = self._models.get(connection_id)
        if stored is None:
            candidates: list[tuple[ModelEntry, model_mod.Tier]] = [
                (e, "standard") for e in catalogue if e.provider == provider
            ]
        else:
            candidates = []
            for row in stored:
                found = catalogue.get(provider, row.model_id) if row.enabled else None
                if found is not None:
                    candidates.append((found, row.tier))
        offered: list[tuple[ModelEntry, model_mod.Tier]] = []
        for entry, tier in candidates:
            if entry.kind != "chat":
                continue
            try:
                connection.residency.check(entry, connection.region, task=connection_id)
            except ResidencyRefused:
                continue
            offered.append((entry, tier))
        return sorted(offered, key=lambda item: item[0].model_id)

    def _describe(self, connection: Connection, entry: ModelEntry, tier: str) -> dict[str, Any]:
        return {
            "id": str(ModelRef(connection.id, entry.model_id)),
            "name": entry.model_id,
            "model_id": entry.model_id,
            "provider": connection.name,
            "connection_id": connection.id,
            "connection_name": connection.name,
            "kind": connection.kind,
            "tier": tier,
            "supports_tools": entry.capabilities.tools,
            "context_length": entry.context_window,
            "max_output": entry.output_limit,
        }

    def listed_models(self, connection_id: str) -> list[dict[str, Any]]:
        """The models one connection offers, described for a page."""
        connection = self.connection(connection_id)
        if connection is None:
            return []
        return [self._describe(connection, e, t) for e, t in self._offered(connection_id)]

    def may_use(self, connection_id: str, user: UserScope | None) -> bool:
        """Whether ``user`` may use the connection (None: personal mode, always)."""
        if user is None:
            return True
        with self._lock:
            assigned = self._assignments.get(connection_id, ())
        if any(a.user_guid == user.user_guid for a in assigned if a.user_guid is not None):
            return True
        if any(a.role in user.roles for a in assigned if a.role is not None):
            return True
        return not assigned and user.administrator

    def offered_models(self, user: UserScope | None = None) -> list[dict[str, Any]]:
        """Every model ``user`` may choose, grouped by connection (by connection id)."""
        with self._lock:
            running = sorted(self._built)
        models: list[dict[str, Any]] = []
        for connection_id in running:
            if self.may_use(connection_id, user):
                models.extend(self.listed_models(connection_id))
        return models

    def resolve(self, model: str, user: UserScope | None = None) -> ModelRef | None:
        """The pair ``model`` names: a ``connection/model`` reference to a configured
        connection, or a bare model id (as stored before connections) on a running
        connection ``user`` may use: the first by connection id that offers it, else
        the first whose catalogue declares it. None otherwise."""
        if not model:
            return None
        parsed = ModelRef.parse(model)
        if parsed is not None and self.connection(parsed.connection_id) is not None:
            return parsed
        with self._lock:
            running = [c for c in sorted(self._built) if self.may_use(c, user)]
        for connection_id in running:
            if any(e.model_id == model for e, _ in self._offered(connection_id)):
                return ModelRef(connection_id, model)
        # A model no longer offered (a conversation pinned to it) keeps working on a
        # connection whose catalogue still declares it within the residency policy.
        for connection_id in running:
            if self.refusal(ModelRef(connection_id, model)) is None:
                return ModelRef(connection_id, model)
        return None

    def is_offered(self, model: str, user: UserScope | None = None) -> bool:
        """Whether ``user`` may choose ``model`` now."""
        ref = self.resolve(model, user)
        if ref is None or not self.may_use(ref.connection_id, user):
            return False
        return any(e.model_id == ref.model_id for e, _ in self._offered(ref.connection_id))

    def limits(self, model: str) -> tuple[int, int]:
        """(context window, output limit) of a model from the catalogue (a fallback when
        the model is not declared)."""
        ref = self.resolve(model)
        provider = self._catalogue_provider(ref.connection_id) if ref is not None else None
        entry = (
            self.catalogue_for(ref.connection_id).get(provider, ref.model_id)
            if ref and provider
            else None
        )
        if entry is None:
            return FALLBACK_LIMITS
        return entry.context_window, entry.output_limit or FALLBACK_LIMITS[1]

    def service(
        self, model: str | ModelRef, *, attribution: Attribution | None = None
    ) -> ConnectionService | None:
        """A service of its own for a model (None when no running connection has it)."""
        ref = model if isinstance(model, ModelRef) else self.resolve(model)
        if ref is None:
            return None
        return ConnectionService(self, ref, attribution=attribution)

    # -- building ----------------------------------------------------------------------

    async def _secrets_digest(self, connection: Connection) -> str:
        digest = hashlib.sha256()
        for ref in _secret_refs(connection.settings):
            try:
                value = (await self._secrets.resolve(ref)).get_secret_value()
            except ConduitError:
                value = ""
            digest.update(f"{ref.name}\0{value}\0".encode())
        return digest.hexdigest()

    async def _build(self, connection: Connection) -> tuple[ProviderSettings, MeteredProvider]:
        registry = self.registry(connection.residency, connection_id=connection.id)
        settings = model_mod.validate(connection, registry)
        provider = await registry.build(connection.id, settings)
        return settings, provider

    async def _apply(self, snapshot: Snapshot) -> None:
        wanted = {c.id: c for c in snapshot.connections}
        catalogues = {
            cid: connection_catalogue(snapshot.overlays, cid, self._catalogue) for cid in wanted
        }
        with self._lock:
            self._catalogues = catalogues
            previous = dict(self._built)
        built: dict[str, _Built] = {}
        failures: dict[str, str] = {}
        retired: list[MeteredProvider] = []
        for connection_id in sorted(wanted):
            connection = wanted[connection_id]
            if not connection.enabled:
                continue
            fingerprint = (
                connection.fingerprint()
                + await self._secrets_digest(connection)
                + _overlay_digest(snapshot.overlays, connection_id)
            )
            old = previous.get(connection_id)
            if old is not None and old.fingerprint == fingerprint:
                built[connection_id] = old
                continue
            try:
                settings, provider = await self._build(connection)
            except (ConduitError, ConnectionInvalid) as refused:
                reason = str(refused)
                failures[connection_id] = reason
                logger.warning("Connection %s could not be started: %s", connection_id, reason)
                if old is not None:
                    built[connection_id] = old  # keep the working provider
                continue
            built[connection_id] = _Built(connection, settings, provider, fingerprint)
            if old is not None:
                retired.append(old.provider)
                logger.info("Connection %s rebuilt after a change", connection_id)
        retired.extend(b.provider for cid, b in previous.items() if cid not in built)
        with self._lock:
            self._built = built
            self._connections = dict(wanted)
            self._models = {k: tuple(v) for k, v in snapshot.models.items()}
            self._assignments = {k: tuple(v) for k, v in snapshot.assignments.items()}
            self._failures = failures
        for provider in retired:
            self._close_later(provider)

    def _close_later(self, provider: MeteredProvider) -> None:
        async def close() -> None:
            try:
                await asyncio.sleep(self._drain_s)
            finally:
                await provider.aclose()

        task = asyncio.get_running_loop().create_task(close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def apply(self, snapshot: Snapshot) -> None:
        """Bring the providers in line with ``snapshot`` (from any thread but conduit's)."""
        if self._closed:
            raise ModelUnavailable("the connection runtime is closed")
        with self._apply_lock:
            self._loop.run(self._apply(snapshot), timeout=self._timeout)

    def refresh(self, load: Callable[[], Snapshot]) -> None:
        """Re-read the configuration with ``load`` and apply it (the settings follower's hook)."""
        self.apply(load())

    def close(self) -> None:
        """Close every provider and, when this runtime owns it, the conduit loop."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            providers = [b.provider for b in self._built.values()]
            self._built = {}

        async def close_all() -> None:
            for task in list(self._closing):
                task.cancel()
            for provider in providers:
                await provider.aclose()

        try:
            self._loop.run(close_all(), timeout=30)
        finally:
            if self._owns_loop:
                self._loop.close()


class CatalogueLimits:
    """Context windows and output limits from the runtime's catalogue, for the compactor."""

    def __init__(self, runtime: ConnectionRuntime) -> None:
        self._runtime = runtime

    def get_context_window(self, model_id: str) -> int:
        return self._runtime.limits(model_id)[0]

    def get_max_output(self, model_id: str) -> int:
        return self._runtime.limits(model_id)[1]


def snapshot_of(
    connections: Iterable[Connection],
    models: Iterable[ConnectionModel] = (),
    assignments: Iterable[repo.Assignment] = (),
    overlays: Iterable[repo.OverlayRow] = (),
) -> Snapshot:
    """A snapshot from flat lists (models and assignments grouped by connection)."""
    grouped: dict[str, list[ConnectionModel]] = {}
    for m in models:
        grouped.setdefault(m.connection_id, []).append(m)
    assigned: dict[str, list[repo.Assignment]] = {}
    for a in assignments:
        assigned.setdefault(a.connection_id, []).append(a)
    return Snapshot(
        connections=tuple(connections),
        models={k: tuple(v) for k, v in grouped.items()},
        assignments={k: tuple(v) for k, v in assigned.items()},
        overlays=tuple(overlays),
    )
