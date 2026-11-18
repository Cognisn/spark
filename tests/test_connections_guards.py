"""The connection guards below the web: a server turn or action on a connection not
assigned to its user is refused, and a residency refusal reaches the user with
conduit's reason. Real runtime on conduit's stub provider; SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conduit import ResidencyPolicy, ResidencyScope

from spark import mode
from spark.connections import Connection, ConnectionRuntime
from spark.core.conversation_manager import ConnectionNotAssigned, ConversationManager
from spark.database import conversations
from spark.database.connections import Assignment
from spark.identity.permissions import ADMIN_CONNECTIONS, SETTINGS_INSTANCE
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.model_helpers import stub_connection, stub_runtime

ADA = "user-ada-example"
MODEL = "lab/stub-echo"


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


def _runtime(sync_db: Any, assignments: list[Assignment]) -> ConnectionRuntime:
    return stub_runtime(sync_db, connections=[stub_connection("lab")], assignments=assignments)


def _turn(sync_db: Any, runtime: ConnectionRuntime, **kwargs: Any) -> dict[str, Any]:
    mgr = ConversationManager(sync_db, runtime, user_guid="process-guid")
    cid = sync_db.call(conversations.create_conversation, "Ada's", MODEL, ADA)
    sync_db.call(conversations.update_conversation, cid, ADA, memory_enabled=0, rag_enabled=0)
    result: dict[str, Any] = mgr.send_message(cid, "hello", ADA, tools=[], **kwargs)
    return result


class TestTurnAssignment:
    @pytest.mark.parametrize(
        "assignment",
        [Assignment("lab", role="staff"), Assignment("lab", user_guid=ADA)],
        ids=["role", "user"],
    )
    def test_an_assigned_user_may_use_the_connection(
        self, sync_db: Any, server_mode: None, assignment: Assignment
    ) -> None:
        runtime = _runtime(sync_db, [assignment])
        try:
            result = _turn(sync_db, runtime, roles={"staff"}, permissions=frozenset())
        finally:
            runtime.close()
        assert result["content"] == "hello"

    def test_an_unassigned_user_is_refused_before_the_model(
        self, sync_db: Any, server_mode: None
    ) -> None:
        runtime = _runtime(sync_db, [Assignment("lab", role="research")])
        try:
            with pytest.raises(ConnectionNotAssigned, match="Stub lab"):
                _turn(sync_db, runtime, roles={"staff"}, permissions=frozenset())
        finally:
            runtime.close()
        assert sync_db.fetch_value("SELECT COUNT(*) FROM usage_tracking") == 0

    def test_an_administrator_may_try_an_unassigned_connection_only(
        self, sync_db: Any, server_mode: None
    ) -> None:
        admin = frozenset({ADMIN_CONNECTIONS})
        runtime = _runtime(sync_db, [])
        try:
            assert _turn(sync_db, runtime, roles=set(), permissions=admin)["content"] == "hello"
            with pytest.raises(ConnectionNotAssigned):
                _turn(sync_db, runtime, roles=set(), permissions=frozenset())
        finally:
            runtime.close()

    def test_instance_settings_alone_do_not_make_an_administrator_of_connections(
        self, sync_db: Any, server_mode: None
    ) -> None:
        runtime = _runtime(sync_db, [])
        try:
            with pytest.raises(ConnectionNotAssigned):
                _turn(sync_db, runtime, roles=set(), permissions=frozenset({SETTINGS_INSTANCE}))
        finally:
            runtime.close()

    def test_personal_mode_uses_every_connection(self, sync_db: Any) -> None:
        mode.reset()
        runtime = _runtime(sync_db, [Assignment("lab", role="research")])
        try:
            assert _turn(sync_db, runtime)["content"] == "hello"
        finally:
            runtime.close()


class TestActionOwnerAssignment:
    def _executor(self, sync_db: Any, roles: set[str]) -> Any:
        from spark.identity.permissions import ACTIONS
        from spark.scheduler.executor import ActionExecutor

        class Ctx:
            settings = None
            secrets = None

        return ActionExecutor(
            Ctx(),
            "worker-1",
            sync_db,
            permissions=frozenset({ACTIONS}),
            user_guid=ADA,
            roles=roles,
        )

    def test_the_owner_must_be_assigned_the_connection(
        self, sync_db: Any, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.connections import startup

        runtime = _runtime(sync_db, [Assignment("lab", role="staff")])
        monkeypatch.setattr(startup, "ensure", lambda **kwargs: runtime)
        try:
            assert self._executor(sync_db, {"staff"})._init_llm(MODEL) is not None
            with pytest.raises(PermissionError, match="not assigned"):
                self._executor(sync_db, {"guest"})._init_llm(MODEL)
            assert self._executor(sync_db, {"staff"})._init_llm("gone/model") is None
        finally:
            runtime.close()


class TestResidencySurfaces:
    def test_the_turn_reports_conduits_reason(self, sync_db: Any) -> None:
        outside = Connection(
            "lab",
            "Lab",
            {"kind": "stub", "not_for_production": True},
            residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.GLOBAL})),
        )
        runtime = stub_runtime(sync_db, connections=[outside])
        try:
            result = _turn(sync_db, runtime)
        finally:
            runtime.close()
        assert result["error"] is True
        assert "scope local is not allowed" in result["content"]
        assert sync_db.fetch_value("SELECT COUNT(*) FROM usage_tracking") == 0
