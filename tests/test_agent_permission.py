"""Spawning a sub-agent needs ``spark.agents`` on a server, wherever the spawn comes from.

The spawn point (``ConversationManager._execute_agent_spawn``) is the one place
every sub-agent starts: chat turns, actions run from the web or the worker, and
their own sub-agents. ``spark.tools.agents`` offers the tool; ``spark.agents``
lets the user spawn. Personal mode's local operator holds every permission.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.identity.permissions import AGENTS
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.tools.registry import permission_name
from tests.test_conversation_manager_users import ADA, RecordingAgentExecutor, _manager

AGENT_TOOLS = permission_name("agents")


@pytest.fixture
def executors(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import spark.core.agent_executor as agent_executor

    RecordingAgentExecutor.users = []
    monkeypatch.setattr(agent_executor, "AgentExecutor", RecordingAgentExecutor)
    return RecordingAgentExecutor.users


@pytest.fixture
def server() -> Iterator[None]:
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y"))
    )
    yield
    mode.reset()


def _spawn(sync_db: Any, permissions: Any) -> tuple[str, bool]:
    mgr = _manager(sync_db, None)
    cid = mgr.create_conversation("Ada's", "stub-model", ADA)
    return mgr._execute_agent_spawn(
        {"task": "research", "agent_name": "Researcher"}, cid, ADA, permissions=permissions
    )


@pytest.mark.usefixtures("server")
class TestServer:
    def test_the_agents_tools_alone_do_not_spawn(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        text, is_error = _spawn(sync_db, frozenset({AGENT_TOOLS}))
        assert is_error
        assert AGENTS in text
        assert executors == []

    def test_no_signed_in_user_never_spawns(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        text, is_error = _spawn(sync_db, None)
        assert is_error and executors == []

    def test_spark_agents_spawns(self, sync_db: Any, executors: list[tuple[str, str]]) -> None:
        text, is_error = _spawn(sync_db, frozenset({AGENT_TOOLS, AGENTS}))
        assert not is_error, text
        assert executors == [(ADA, "research")]


def test_personal_mode_spawns_as_before(sync_db: Any, executors: list[tuple[str, str]]) -> None:
    mode.reset()
    text, is_error = _spawn(sync_db, None)
    assert not is_error, text
    assert executors == [(ADA, "research")]
