"""Test stand-ins for SPARK's model connections.

:class:`FakeModels` implements what the orchestrators ask of the connection runtime
(:class:`spark.connections.runtime.ModelSource`) over a canned service in SPARK's
response format, for tests about orchestration rather than conduit. Model ids may
be bare (``stub-model``) or ``connection/model``; both resolve to the one fake
connection, ``test``.

:func:`stub_runtime` is a real :class:`~spark.connections.runtime.ConnectionRuntime`
on conduit's stub provider (deterministic, no network), with its usage sink writing
to a database bridge, for tests of metering and of conduit's own behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from conduit import LiteralSecretResolver, ResidencyPolicy, ResidencyScope
from conduit.providers.stub import StubFactory, StubScript

from spark.connections import Connection, ConnectionRuntime, ModelRef, UserScope
from spark.connections.runtime import snapshot_of
from spark.connections.usage import Attribution, UsageSink, database_recorder
from spark.database.connections import Assignment

CONNECTION = "test"
STUB_MODEL = "stub-echo"


class FixedLimits:
    """Context limits for a model the catalogue does not know (the runtime's fallback)."""

    CONTEXT_WINDOW = 8_192
    MAX_OUTPUT = 4_096

    def get_context_window(self, model_id: str) -> int:
        return self.CONTEXT_WINDOW

    def get_max_output(self, model_id: str) -> int:
        return self.MAX_OUTPUT


class FakeModels:
    """A ModelSource over one fake connection whose every model answers with ``service``."""

    def __init__(
        self,
        service: Any = None,
        *,
        factory: Callable[[str], Any] | None = None,
        models: Sequence[dict[str, Any]] | None = None,
        connection_id: str = CONNECTION,
        allowed: Callable[[str, UserScope | None], bool] | None = None,
    ) -> None:
        self._service = service
        self._factory = factory
        self._models = list(models) if models is not None else None
        self.connection_id = connection_id
        self._allowed = allowed
        self.attributions: list[Attribution | None] = []
        self.requested: list[str] = []

    def _model_id(self, model: str) -> str:
        parsed = ModelRef.parse(model)
        return parsed.model_id if parsed is not None else model

    def service(self, model: str | ModelRef, *, attribution: Attribution | None = None) -> Any:
        text = str(model)
        self.requested.append(text)
        self.attributions.append(attribution)
        if self._factory is not None:
            return self._factory(self._model_id(text))
        return self._service

    def resolve(self, model: str, user: UserScope | None = None) -> ModelRef | None:
        if not model:
            return None
        parsed = ModelRef.parse(model)
        return parsed if parsed is not None else ModelRef(self.connection_id, model)

    def limits(self, model: str) -> tuple[int, int]:
        return FixedLimits.CONTEXT_WINDOW, FixedLimits.MAX_OUTPUT

    def listed_models(self, connection_id: str) -> list[dict[str, Any]]:
        if self._models is not None:
            return list(self._models)
        if self._service is not None and hasattr(self._service, "list_available_models"):
            return [
                {**m, "model_id": m.get("id"), "connection_id": connection_id}
                for m in self._service.list_available_models()
            ]
        return []

    def may_use(self, connection_id: str, user: UserScope | None) -> bool:
        return self._allowed(connection_id, user) if self._allowed is not None else True

    def connection_name(self, connection_id: str) -> str:
        return "Test connection"

    # What the web layer asks (spark.web.models)
    def offered_models(self, user: UserScope | None = None) -> list[dict[str, Any]]:
        return self.listed_models(self.connection_id)

    def is_offered(self, model: str, user: UserScope | None = None) -> bool:
        if self._models is None:
            return True
        wanted = self._model_id(model)
        return any(m.get("id") in (model, wanted) for m in self._models)

    def connections(self) -> list[Connection]:
        return []

    def failures(self) -> dict[str, str]:
        return {}

    def close(self) -> None:
        """Nothing to close (the app's shutdown closes its runtime)."""


def stub_turn(response: dict[str, Any]) -> Any:
    """A conduit stub turn playing a response written in SPARK's format."""
    from conduit import Usage
    from conduit.providers.stub import StubToolCall, StubTurn

    usage = response.get("usage") or {}
    calls = tuple(
        StubToolCall(name=c["name"], input=dict(c.get("input") or {}), id=c.get("id"))
        for c in (response.get("tool_use") or [])
    )
    return StubTurn(
        text=str(response.get("content") or ""),
        tool_calls=calls,
        usage=Usage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
    )


class ScriptedModels:
    """One stub connection whose models (declared by catalogue overlay) each play
    SPARK-format responses in order.

    ``factory`` is a service factory for orchestrators (a bare model id, as their
    tests store it, resolves to the stub connection); ``calls`` counts each model's
    calls. A model's responses list may be appended to after the runtime starts.
    """

    def __init__(self, sync_db: Any, scripts: dict[str, list[dict[str, Any]]]) -> None:
        from spark.database.connections import OverlayRow

        self.responses = {model: list(r) for model, r in scripts.items()}
        self.calls = {model: 0 for model in scripts}

        def script(request: Any, index: int) -> Any:
            self.calls[request.model] += 1
            return stub_turn(self.responses[request.model].pop(0))

        self.runtime = stub_runtime(
            sync_db,
            connections=[stub_connection("stub", script="scripted")],
            scripts={"scripted": script},
            overlays=[
                OverlayRow("stub", model, 200_000, "local", output_limit=8_192, tools=True)
                for model in scripts
            ],
        )

    def factory(self, model: str) -> Any:
        return self.runtime.service(model)

    def close(self) -> None:
        self.runtime.close()


def stub_connection(
    connection_id: str = "stub", *, script: str = "echo", enabled: bool = True
) -> Connection:
    """A connection on conduit's stub provider (tests only)."""
    return Connection(
        id=connection_id,
        name=f"Stub {connection_id}",
        settings={"kind": "stub", "not_for_production": True, "script": script},
        residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL})),
        enabled=enabled,
    )


def stub_runtime(
    sync_db: Any = None,
    *,
    connections: Iterable[Connection] | None = None,
    scripts: dict[str, StubScript] | None = None,
    assignments: Iterable[Assignment] = (),
    overlays: Iterable[Any] = (),
    models: Iterable[Any] = (),
) -> ConnectionRuntime:
    """A real connection runtime on conduit's stub provider, recording usage to ``sync_db``."""
    runtime = ConnectionRuntime(
        secrets=LiteralSecretResolver({}),
        usage=UsageSink(database_recorder(sync_db)) if sync_db is not None else None,
        factories=[StubFactory(scripts=scripts or {})],
        drain_s=0.01,
    )
    chosen = list(connections) if connections is not None else [stub_connection()]
    runtime.apply(snapshot_of(chosen, models, assignments, overlays))
    return runtime
