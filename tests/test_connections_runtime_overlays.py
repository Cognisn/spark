"""A connection's provider follows the catalogue it was built with (wave 10 Q54).

conduit's registry-built provider keeps the catalogue it was built with and checks
every call against it, so a model declared, declared again or removed on a running
connection must rebuild that connection's provider, on every runtime that hears of
the change, and on no other connection. Covered here on conduit's stub provider:
the runtime alone, and through the stored configuration (server mode on SQLite and
PostgreSQL, personal mode on ``config.yaml``) re-read by the settings follower's
hook on two runtimes (a replica or the worker).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conduit import LiteralSecretResolver, ResidencyPolicy, ResidencyScope
from conduit.providers.stub import StubFactory

from spark.connections import Connection, ConnectionRuntime, ModelRefused
from spark.connections.runtime import Snapshot, snapshot_of
from spark.connections.startup import Refresher, loader
from spark.connections.store import DatabaseConnectionStore, FileConnectionStore
from spark.connections.usage import UsageSink
from spark.database import connections as repo

DECLARED = "stub-local-declared"


def stub(cid: str, **extra: Any) -> Connection:
    return Connection(
        id=cid,
        name=f"Stub {cid}",
        settings={"kind": "stub", "not_for_production": True, "script": "echo", **extra},
        residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL})),
    )


def declared(cid: str, *, context: int = 4_096, output: int = 1_024) -> repo.OverlayRow:
    return repo.OverlayRow(
        "stub", DECLARED, context, "local", output_limit=output, tools=True, connection_id=cid
    )


def other(cid: str) -> repo.OverlayRow:
    return repo.OverlayRow(
        "stub", "stub-local-other", 2_048, "local", output_limit=512, connection_id=cid
    )


async def _discard(record: Any) -> None:
    return None


def make_runtime() -> ConnectionRuntime:
    return ConnectionRuntime(
        secrets=LiteralSecretResolver({}),
        usage=UsageSink(_discard),
        factories=[StubFactory()],
        drain_s=0.01,
    )


@pytest.fixture
def runtime() -> Iterator[ConnectionRuntime]:
    rt = make_runtime()
    try:
        yield rt
    finally:
        rt.close()


def ask(rt: ConnectionRuntime, model: str, text: str = "declared at once") -> str:
    service = rt.service(model)
    assert service is not None
    content = service.invoke_model([{"role": "user", "content": text}])["content"]
    assert isinstance(content, str)
    return content


class TestRuntime:
    def test_a_model_declared_on_a_running_connection_can_be_used_at_once(
        self, runtime: ConnectionRuntime
    ) -> None:
        runtime.apply(snapshot_of([stub("home"), stub("lab")]))
        home, lab = runtime.provider("home"), runtime.provider("lab")
        runtime.apply(snapshot_of([stub("home"), stub("lab")], overlays=[declared("home")]))
        assert ask(runtime, f"home/{DECLARED}") == "declared at once"
        assert runtime.provider("home") is not home
        assert runtime.provider("lab") is lab  # its catalogue did not change

    def test_a_declaration_made_again_rebuilds_with_the_new_entry(
        self, runtime: ConnectionRuntime
    ) -> None:
        runtime.apply(snapshot_of([stub("home"), stub("lab")], overlays=[declared("home")]))
        home, lab = runtime.provider("home"), runtime.provider("lab")
        again = declared("home", context=16_384, output=2_048)
        runtime.apply(snapshot_of([stub("home"), stub("lab")], overlays=[again]))
        assert runtime.provider("home") is not home
        assert runtime.provider("lab") is lab
        assert runtime.limits(f"home/{DECLARED}") == (16_384, 2_048)
        assert ask(runtime, f"home/{DECLARED}", "again") == "again"

    def test_a_removed_declaration_is_refused_by_the_provider(
        self, runtime: ConnectionRuntime
    ) -> None:
        runtime.apply(snapshot_of([stub("home"), stub("lab")], overlays=[declared("home")]))
        lab = runtime.provider("lab")
        runtime.apply(snapshot_of([stub("home"), stub("lab")]))
        assert runtime.provider("lab") is lab
        with pytest.raises(ModelRefused, match="not declared"):
            ask(runtime, f"home/{DECLARED}")

    def test_another_connections_declaration_leaves_this_provider_alone(
        self, runtime: ConnectionRuntime
    ) -> None:
        runtime.apply(snapshot_of([stub("home"), stub("lab")], overlays=[declared("home")]))
        home = runtime.provider("home")
        runtime.apply(
            snapshot_of([stub("home"), stub("lab")], overlays=[declared("home"), other("lab")])
        )
        assert runtime.provider("home") is home
        assert ask(runtime, "lab/stub-local-other", "lab") == "lab"

    def test_the_same_entries_in_another_order_rebuild_nothing(
        self, runtime: ConnectionRuntime
    ) -> None:
        entries = [declared("home"), other("home")]
        runtime.apply(snapshot_of([stub("home")], overlays=entries))
        home = runtime.provider("home")
        runtime.apply(snapshot_of([stub("home")], overlays=list(reversed(entries))))
        assert runtime.provider("home") is home


# -- through the stored configuration and the settings follower's hook ------------------


class _Stores:
    """Writes to the stored configuration, as the overlay endpoint does."""

    def __init__(self, bridge: Any, file: FileConnectionStore | None) -> None:
        self._bridge = bridge
        self._file = file

    def _store(self, db: Any) -> Any:
        return self._file if self._file is not None else DatabaseConnectionStore(db)

    def save_connection(self, connection: Connection) -> None:
        async def run(db: Any) -> None:
            await self._store(db).save_connection(connection)

        self._bridge.call(run)

    def save_overlay(self, entry: repo.OverlayRow) -> None:
        async def run(db: Any) -> None:
            await self._store(db).save_overlay(entry)

        self._bridge.call(run)


def _replicas_hear_of_a_declaration(
    stores: _Stores, loads: list[Any], *, cleanup: list[ConnectionRuntime]
) -> None:
    stores.save_connection(stub("home"))
    stores.save_connection(stub("lab"))
    runtimes: list[tuple[ConnectionRuntime, Refresher]] = []
    for load in loads:
        rt = make_runtime()
        cleanup.append(rt)
        rt.apply(load())
        runtimes.append((rt, Refresher(rt, load)))
    before = [(rt.provider("home"), rt.provider("lab")) for rt, _ in runtimes]

    stores.save_overlay(declared("home"))  # the administrator declares the model
    for rt, follower in runtimes:  # each runtime's settings follower hears the notice
        follower.refresh()

    for (rt, _), (home, lab) in zip(runtimes, before, strict=True):
        assert rt.provider("home") is not home
        assert rt.provider("lab") is lab
        assert ask(rt, f"home/{DECLARED}") == "declared at once"


def _snapshot_loader(bridge: Any, *, server: bool, path: Path | None) -> Any:
    load = loader(bridge, server=server, config_path=path)

    def read() -> Snapshot:
        snapshot = load()
        assert isinstance(snapshot, Snapshot)
        return snapshot

    return read


def test_server_runtimes_rebuild_on_the_change_notice(spark_db_settings: Any) -> None:
    from spark.db.standalone import open_standalone

    web = open_standalone(spark_db_settings, auto_migrate=False)
    worker = open_standalone(spark_db_settings, auto_migrate=False)
    cleanup: list[ConnectionRuntime] = []
    try:
        _replicas_hear_of_a_declaration(
            _Stores(web.sync_db, None),
            [
                _snapshot_loader(web.sync_db, server=True, path=None),
                _snapshot_loader(worker.sync_db, server=True, path=None),
            ],
            cleanup=cleanup,
        )
    finally:
        for rt in cleanup:
            rt.close()
        worker.close()
        web.close()


def test_personal_runtimes_rebuild_on_a_refresh(sync_db: Any, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cleanup: list[ConnectionRuntime] = []
    try:
        _replicas_hear_of_a_declaration(
            _Stores(sync_db, FileConnectionStore(path)),
            [
                _snapshot_loader(sync_db, server=False, path=path),
                _snapshot_loader(sync_db, server=False, path=path),
            ],
            cleanup=cleanup,
        )
    finally:
        for rt in cleanup:
            rt.close()
