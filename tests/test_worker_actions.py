"""Server mode: the worker runs every user's enabled actions, each as its owner (D22, A5).

A real PostgreSQL clone at head and a real warden runtime hold four synthetic
owners: a power user, a user without ``spark.actions``, a suspended power user,
and a narrow role holding ``spark.actions`` but no web tools. The LLM is a
script that stores a memory, writes a Word document and fetches a web page.

Each run executes as its owner: the run record, the memory index and the file
area are the owner's, and the tools follow the owner's warden permissions,
read at run time. An owner without ``spark.actions``, a suspended or
deactivated owner and a guid with no warden user are refused and the refusal is
recorded as a run. The process guid of the shared secrets store is never asked.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from warden.audit import set_recorder
from warden.identity import UserStatus

from spark import files as files_pkg
from spark import mode
from spark.database import autonomous_actions, user_identities
from spark.db import Database, SyncDatabase
from spark.identity import permissions as perms
from spark.identity.permissions import ACTIONS, POWER_USER, USER, RoleSpec
from spark.identity.runtime import IdentityRuntime
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.scheduler.owners import WardenOwners
from spark.tools.registry import ANY_ROLE, permission_name
from tests.test_identity_runtime import _settings
from tests.test_instance_settings_web import MemorySecrets

pytestmark = pytest.mark.postgres

NARROW = "scheduler_narrow"
CRON = '{"cron": "0 3 * * *"}'


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def no_process_guid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared-secrets user guid must never be asked for in server mode."""
    import spark.core.user_guid as user_guid

    def _refuse(ctx: object | None = None) -> str:
        raise AssertionError("get_user_guid called in server mode")

    monkeypatch.setattr(user_guid, "get_user_guid", _refuse)


@pytest.fixture(autouse=True)
def narrow_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A role holding spark.actions with memory, documents and core tools, but no web tools."""
    roles = dict(perms.ROLES)
    roles[NARROW] = RoleSpec(
        frozenset(
            {
                ACTIONS,
                permission_name("memory"),
                permission_name("documents"),
                permission_name("core"),
            }
        )
    )
    monkeypatch.setattr(perms, "ROLES", roles)


@pytest.fixture
def server_db(pg_server: str, _pg_template: str) -> Iterator[DatabaseSettings]:
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=f"{base}/{name}")
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def server_mode(server_db: DatabaseSettings, tmp_path: Path) -> Iterator[Path]:
    mode.configure(RuntimeSettings(Mode.SERVER, server_db))
    volume = tmp_path / "volume"
    files_pkg.configure(
        _Settings({"files.root": str(volume)}), data_dir=tmp_path, runtime_mode=Mode.SERVER
    )
    try:
        yield volume
    finally:
        files_pkg.reset()
        mode.reset()


@dataclass
class Stack:
    db: Database
    sync_db: SyncDatabase
    runtime: IdentityRuntime
    owners: WardenOwners
    volume: Path
    guids: dict[str, str]


async def _owner(runtime: IdentityRuntime, db: Database, email: str, *roles: str) -> str:
    identity = runtime.identity
    user = await identity.users.create(email, actor="test", password="dev-only-password-1")
    for role in roles:
        await identity.roles.assign(user.id, role, actor="test")
    return await user_identities.user_guid_for(db, str(user.id))


@pytest.fixture
async def stack(server_db: DatabaseSettings, server_mode: Path) -> AsyncIterator[Stack]:
    previous = set_recorder(None)
    db = await Database.open(server_db)
    runtime = await IdentityRuntime.start(db, _settings(), MemorySecrets())
    try:
        guids = {
            "power": await _owner(runtime, db, "power@example.com", POWER_USER),
            "plain": await _owner(runtime, db, "plain@example.com", USER),
            "narrow": await _owner(runtime, db, "narrow@example.com", NARROW),
            "suspended": await _owner(runtime, db, "suspended@example.com", POWER_USER),
            "deactivated": await _owner(runtime, db, "gone@example.com", POWER_USER),
        }
        for key, status in (
            ("suspended", UserStatus.SUSPENDED),
            ("deactivated", UserStatus.DEACTIVATED),
        ):
            warden_id = await user_identities.warden_user_for(db, guids[key])
            assert warden_id is not None
            await runtime.identity.users.set_status(uuid.UUID(warden_id), status, actor="test")
        yield Stack(
            db, SyncDatabase(db), runtime, WardenOwners(runtime.identity, db), server_mode, guids
        )
    finally:
        runtime.close()
        await db.close()
        set_recorder(previous)


class ScriptedLLM:
    """Store a memory, write a Word document, fetch a page, then finish."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke_model(self, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {
                "stop_reason": "tool_use",
                "content": "",
                "content_blocks": [],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "tool_use": [
                    {"id": "t1", "name": "store_memory", "input": {"content": "tea at three"}},
                    {
                        "id": "t2",
                        "name": "create_word",
                        "input": {
                            "path": "report.docx",
                            "content": [{"type": "paragraph", "text": "nightly report"}],
                        },
                    },
                    {"id": "t3", "name": "web_fetch", "input": {"url": "https://example.com"}},
                ],
            }
        return {
            "stop_reason": "end_turn",
            "content": "done",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }


@dataclass
class Seen:
    llms: list[ScriptedLLM]
    memory_users: list[str]
    memories: list[tuple[str, str]]
    fetched: list[str]


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> Seen:
    import spark.index.memory_index as memory_index
    import spark.tools.web as web
    from spark.scheduler.executor import ActionExecutor

    record = Seen([], [], [], [])

    def _llm(self: Any, model_id: str) -> ScriptedLLM:
        llm = ScriptedLLM()
        record.llms.append(llm)
        return llm

    class FakeMemoryIndex:
        def __init__(self, db: Any, user_guid: str) -> None:
            self.user_guid = user_guid
            record.memory_users.append(user_guid)

        def store(self, content: str, category: str, **_: Any) -> int:
            record.memories.append((self.user_guid, content))
            return len(record.memories)

    def _fetch(tool_name: str, tool_input: dict[str, Any], config: Any) -> str:
        record.fetched.append(tool_input["url"])
        return "fetched"

    monkeypatch.setattr(ActionExecutor, "_init_llm", _llm)
    monkeypatch.setattr(ActionExecutor, "_init_mcp", lambda self: None)
    monkeypatch.setattr(memory_index, "MemoryIndex", FakeMemoryIndex)
    monkeypatch.setattr(web, "execute", _fetch)
    return record


class Ctx:
    """The konfig context the runner and executor read settings from."""

    def __init__(self) -> None:
        self.settings = _Settings(
            {
                "embedded_tools": {
                    "web": {"enabled": True},
                    "documents": {"enabled": True, "mode": "read_write"},
                },
                "conversation.prompt_caching": False,
            }
        )
        self.secrets = None


def _runner(stack: Stack) -> Any:
    from spark.scheduler.runner import ActionRunner

    return ActionRunner(Ctx(), "worker-test", stack.sync_db, owners=stack.owners)


async def _action(stack: Stack, owner: str, name: str = "nightly") -> int:
    return await autonomous_actions.create_action(
        stack.db,
        name,
        "write the nightly report",
        "model-a",
        stack.guids.get(owner, owner),
        schedule_type="recurring",
        schedule_config=CRON,
    )


async def _runs(stack: Stack, owner: str, aid: int) -> list[Any]:
    return await autonomous_actions.get_action_runs(stack.db, stack.guids.get(owner, owner), aid)


async def _execute(stack: Stack, aid: int, owner: str) -> None:
    runner = _runner(stack)
    await asyncio.to_thread(runner._execute_action, aid, stack.guids.get(owner, owner))


class TestRunsAsOwner:
    async def test_a_power_users_action_runs_as_its_owner(self, stack: Stack, seen: Seen) -> None:
        aid = await _action(stack, "power")
        other = await _action(stack, "narrow")  # same name, another owner
        await _execute(stack, aid, "power")
        owner = stack.guids["power"]

        runs = await _runs(stack, "power", aid)
        assert [(r["status"], r["user_guid"]) for r in runs] == [("completed", owner)]
        assert await _runs(stack, "narrow", other) == []
        assert seen.memory_users and set(seen.memory_users) == {owner}
        assert seen.memories == [(owner, "tea at three")]
        written = files_pkg.file_area(stack.sync_db).store.area(owner) / "report.docx"
        assert written.is_file()
        recorded = await stack.db.fetch_all("SELECT user_guid FROM user_files")
        assert [r["user_guid"] for r in recorded] == [owner]
        assert seen.fetched == ["https://example.com"]
        action = await autonomous_actions.get_action(stack.db, aid, owner)
        assert action is not None and action["last_run_at"] is not None

    async def test_tools_follow_the_owners_role(self, stack: Stack, seen: Seen) -> None:
        aid = await _action(stack, "narrow")
        await _execute(stack, aid, "narrow")
        owner = stack.guids["narrow"]
        runs = await _runs(stack, "narrow", aid)
        assert [r["status"] for r in runs] == ["completed"]
        assert seen.fetched == []  # the narrow role holds no web tools
        assert "your role does not allow web tools" in runs[0]["result_text"]
        assert seen.memories == [(owner, "tea at three")]
        assert (files_pkg.file_area(stack.sync_db).store.area(owner) / "report.docx").is_file()

    async def test_permissions_are_read_at_run_time(self, stack: Stack, seen: Seen) -> None:
        aid = await _action(stack, "power")
        warden_id = await user_identities.warden_user_for(stack.db, stack.guids["power"])
        assert warden_id is not None
        await stack.runtime.identity.roles.revoke(uuid.UUID(warden_id), POWER_USER, actor="test")
        await stack.runtime.identity.roles.assign(uuid.UUID(warden_id), USER, actor="test")
        await _execute(stack, aid, "power")
        runs = await _runs(stack, "power", aid)
        assert [r["status"] for r in runs] == ["refused"]
        assert seen.llms == []


class TestRefusals:
    @pytest.mark.parametrize(
        ("owner", "reason"),
        [
            ("plain", "spark.actions"),
            ("suspended", "suspended"),
            ("deactivated", "deactivated"),
            ("no-warden-user-guid", "no warden user"),
        ],
    )
    async def test_refused_and_recorded(
        self, stack: Stack, seen: Seen, owner: str, reason: str
    ) -> None:
        aid = await _action(stack, owner)
        await _execute(stack, aid, owner)
        runs = await _runs(stack, owner, aid)
        assert [r["status"] for r in runs] == ["refused"]
        assert reason in runs[0]["error_message"]
        assert runs[0]["user_guid"] == stack.guids.get(owner, owner)
        assert seen.llms == [] and seen.memory_users == [] and seen.fetched == []
        action = await autonomous_actions.get_action(stack.db, aid, stack.guids.get(owner, owner))
        assert action is not None
        assert action["is_enabled"] == 1 and action["last_run_at"] is None

    async def test_without_an_owner_resolver_every_action_is_refused(
        self, stack: Stack, seen: Seen
    ) -> None:
        from spark.scheduler.runner import ActionRunner

        aid = await _action(stack, "power")
        runner = ActionRunner(Ctx(), "worker-test", stack.sync_db)
        await asyncio.to_thread(runner._execute_action, aid, stack.guids["power"])
        runs = await _runs(stack, "power", aid)
        assert [r["status"] for r in runs] == ["refused"]
        assert seen.llms == []


class TestScheduling:
    async def test_every_users_enabled_actions_are_scheduled_for_their_owner(
        self, stack: Stack
    ) -> None:
        mine = {
            await _action(stack, "power"): stack.guids["power"],
            await _action(stack, "narrow"): stack.guids["narrow"],
        }
        off = await _action(stack, "plain")
        await autonomous_actions.update_action(stack.db, off, stack.guids["plain"], is_enabled=0)
        runner = _runner(stack)
        await asyncio.to_thread(runner._reload_all)
        jobs = {job.id: list(job.args) for job in runner._scheduler.get_jobs()}
        assert jobs == {f"action_{aid}": [aid, owner] for aid, owner in mine.items()}
        for aid, owner in mine.items():
            action = await autonomous_actions.get_action(stack.db, aid, owner)
            assert action is not None and action["next_run_at"] is not None


class TestExecutor:
    def test_a_server_executor_needs_its_owner(self, server_mode: Path) -> None:
        from spark.scheduler.executor import ActionExecutor

        with pytest.raises(ValueError, match="owner"):
            ActionExecutor(Ctx(), "worker-test", object(), permissions=ANY_ROLE)  # type: ignore[arg-type]


class TestWorkerService:
    async def test_the_worker_builds_warden_and_schedules_as_owners(
        self, stack: Stack, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark import worker
        from spark.scheduler import runner as runner_module

        aid = await _action(stack, "power")
        started: list[Any] = []
        real = runner_module.ActionRunner

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                started.append(self)

        monkeypatch.setattr(runner_module, "ActionRunner", Recording)

        class App:
            settings = _settings()
            secrets = MemorySecrets()

        ctx = worker.WorkerContext("worker-test", stack.db, stack.sync_db, App())
        async with worker._scheduled_actions(ctx):
            pass
        assert len(started) == 1
        assert isinstance(started[0]._owners, WardenOwners)
        action = await autonomous_actions.get_action(stack.db, aid, stack.guids["power"])
        assert action is not None and action["next_run_at"] is not None


class TestBudgets:
    """The owner's monthly token budget, decided by their warden roles read at run time (A14)."""

    async def test_every_call_is_billed_to_the_owner(self, stack: Stack, seen: Seen) -> None:
        from spark.database import usage

        aid = await _action(stack, "power")
        await _execute(stack, aid, "power")
        rows = await stack.db.fetch_all(
            "SELECT user_guid, source, action_id, input_tokens, output_tokens"
            " FROM usage_tracking ORDER BY id"
        )
        owner = stack.guids["power"]
        assert rows == [
            {"user_guid": owner, "source": "action", "action_id": aid,
             "input_tokens": 10, "output_tokens": 5},
            {"user_guid": owner, "source": "action", "action_id": aid,
             "input_tokens": 3, "output_tokens": 2},
        ]  # fmt: skip
        period = usage.period_of(datetime.now(timezone.utc))
        assert await usage.tokens_in_period(stack.db, owner, period) == 20

    async def test_an_owner_past_their_roles_budget_is_refused(
        self, stack: Stack, seen: Seen
    ) -> None:
        from spark.database import usage

        owner = stack.guids["power"]
        await usage.set_role_budget(stack.db, POWER_USER, 100)
        await usage.record_usage(stack.db, None, "model-a", 100, 0, owner)
        aid = await _action(stack, "power")
        await _execute(stack, aid, "power")
        runs = await _runs(stack, "power", aid)
        assert [r["status"] for r in runs] == ["refused"]
        assert "monthly token budget" in runs[0]["error_message"]
        assert seen.llms == []

    async def test_a_budget_on_a_role_the_owner_does_not_hold_does_not_apply(
        self, stack: Stack, seen: Seen
    ) -> None:
        from spark.database import usage

        owner = stack.guids["power"]
        await usage.set_role_budget(stack.db, USER, 100)
        await usage.record_usage(stack.db, None, "model-a", 100, 0, owner)
        aid = await _action(stack, "power")
        await _execute(stack, aid, "power")
        assert [r["status"] for r in await _runs(stack, "power", aid)] == ["completed"]

    async def test_owners_resolve_with_their_roles(self, stack: Stack) -> None:
        access = await stack.owners.resolve(stack.guids["narrow"])
        assert access.roles == frozenset({NARROW})
