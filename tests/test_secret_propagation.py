"""Secrets saved on one web replica reach every replica (wave 3 question 16; A8, A9).

Guards under test: saving or clearing a secret sends a settings notification
whose payload is the secret's key and never its value; on that notification
every replica asks konfig to refresh its secrets store and its settings
consumers follow; the value is never in a notification or in the database.

The PostgreSQL tests use two genuinely separate app instances (each its own
app, pool, listener and konfig ``Secrets``) sharing one encrypted file, or one
AWS Secrets Manager bundle whose read cache only a refresh clears.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg
import pytest
import yaml
from conduit import ProviderSettings, SecretRef
from fastapi.testclient import TestClient
from konfig.secrets.secrets import Secrets
from konfig.settings.settings import Settings

import spark.web.server as server
from spark import mode
from spark.identity.permissions import ADMINISTRATOR
from spark.instance_settings import access, accessor, audit
from spark.instance_settings import listener as listener_mod
from spark.instance_settings.listener import SettingsListener
from spark.instance_settings.registry import SettingsError
from spark.instance_settings.repository import CHANNEL
from spark.instance_settings.secrets import announce, is_secret_key, refresher
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.web.server import attach_database, create_app
from tests.identity_helpers import AsIdentity, Holder, identity_for
from tests.web_helpers import sign_in

BOUND_SECONDS = 5.0
MASTER_KEY = "master-key-propagation-dev-only"
ARN = "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark-propagation-dev-only"
# A model connection's credential, in the shared store under its connection name.
LAB_KEY = "connections.lab.api_key"
ANTHROPIC = "voice.elevenlabs.api_key"
XAI = "embedded_tools.web.bing_api_key"
BRAVE = "embedded_tools.web.brave_api_key"
VOICE = "voice.elevenlabs.api_key"

# konfig 0.5.0rc1 and later reload the encrypted file and lock its writes.
needs_konfig_reload = pytest.mark.skipif(
    not hasattr(Secrets, "refresh"),
    reason="needs a cognisn-konfig whose encrypted store reloads and locks (0.5.0rc1 or later)",
)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()
    accessor.reset()
    access.reset()
    audit.set_audit_hook(None)


# -- What is announced ------------------------------------------------------------------


class Recorder:
    """An executor that records what it is asked to run."""

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((sql, params))


class TestAnnounce:
    async def test_postgres_notifies_the_settings_channel_with_the_key_only(self) -> None:
        db = Recorder(Dialect.POSTGRESQL)
        await announce(db, ANTHROPIC)  # type: ignore[arg-type]
        assert db.calls == [("SELECT pg_notify(?, ?)", (CHANNEL, ANTHROPIC))]

    async def test_sqlite_has_no_one_to_tell(self) -> None:
        db = Recorder(Dialect.SQLITE)
        await announce(db, ANTHROPIC)  # type: ignore[arg-type]
        assert db.calls == []

    @pytest.mark.parametrize("payload", ["voice.engine", "not.a.key", "sk-ant-value-dev-only"])
    async def test_refuses_anything_but_a_secret_key(self, payload: str) -> None:
        db = Recorder(Dialect.POSTGRESQL)
        with pytest.raises(SettingsError):
            await announce(db, payload)  # type: ignore[arg-type]
        assert db.calls == []

    def test_is_secret_key(self) -> None:
        assert is_secret_key(ANTHROPIC) and is_secret_key(VOICE)
        # A model connection's credential is one too, so its save refreshes the store.
        assert is_secret_key(LAB_KEY) and is_secret_key("connections.lab.auth.secret_access_key")
        assert not is_secret_key("connections.Lab/x.api_key")
        assert not is_secret_key("voice.engine")
        assert not is_secret_key("")
        assert not is_secret_key("unknown.key")


class TestRefresher:
    def test_a_store_that_can_refresh(self) -> None:
        class Store:
            refreshed = 0

            def refresh(self) -> None:
                self.refreshed += 1

        store = Store()
        refresh = refresher(store)
        assert refresh is not None
        refresh()
        assert store.refreshed == 1

    def test_a_store_without_refresh_or_no_store(self) -> None:
        assert refresher(object()) is None
        assert refresher(None) is None


# -- The listener, with a stand-in connection ---------------------------------------------


@dataclass
class Notify:
    payload: str
    channel: str = CHANNEL


class FakeConnection:
    """Delivers the queued notifications once, then stays idle."""

    def __init__(self, payloads: list[str]) -> None:
        self._queue = [Notify(p) for p in payloads]
        self.executed: list[str] = []

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def notifies(self, timeout: float) -> AsyncIterator[Notify]:
        queue, self._queue = self._queue, []
        for notify in queue:
            yield notify
        if not queue:
            await asyncio.sleep(timeout)

    async def close(self) -> None:
        pass


class Events:
    def __init__(self) -> None:
        self.log: list[str] = []

    async def reload(self) -> None:
        self.log.append("reload")

    def refresh(self) -> None:
        self.log.append("refresh")


async def _run_listener(payloads: list[str], events: Events, settle: int) -> SettingsListener:
    conn = FakeConnection(payloads)

    async def connect(url: str) -> Any:
        return conn

    listener = SettingsListener(
        "postgresql://listener.example.com/spark",
        events.reload,
        refresh_secrets=events.refresh,
        idle_timeout=0.05,
        connect=connect,
    )
    listener.start()
    deadline = time.monotonic() + BOUND_SECONDS
    while events.log.count("reload") < settle and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await listener.stop()
    return listener


class TestListenerRefresh:
    async def test_a_secret_notification_refreshes_the_store_before_reloading(self) -> None:
        events = Events()
        await _run_listener([ANTHROPIC], events, settle=2)
        # connect: refresh then the full load; the notification: refresh then reload.
        assert events.log == ["refresh", "reload", "refresh", "reload"]

    async def test_a_setting_notification_reloads_without_a_refresh(self) -> None:
        events = Events()
        await _run_listener(["voice.engine"], events, settle=2)
        assert events.log == ["refresh", "reload", "reload"]

    async def test_a_failed_refresh_still_reloads_and_keeps_listening(self) -> None:
        log: list[str] = []

        async def reload() -> None:
            log.append("reload")

        def refresh() -> None:
            log.append("refresh")
            raise OSError("store unreachable (dev-only)")

        conn = FakeConnection([ANTHROPIC, "voice.engine"])

        async def connect(url: str) -> Any:
            return conn

        listener = SettingsListener(
            "postgresql://listener.example.com/spark",
            reload,
            refresh_secrets=refresh,
            idle_timeout=0.05,
            connect=connect,
        )
        listener.start()
        deadline = time.monotonic() + BOUND_SECONDS
        while log.count("reload") < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await listener.stop()
        assert log == ["refresh", "reload", "refresh", "reload", "reload"]
        assert listener.reconnects == 0

    async def test_without_a_refresher_notifications_still_reload(self) -> None:
        log: list[str] = []

        async def reload() -> None:
            log.append("reload")

        conn = FakeConnection([ANTHROPIC])

        async def connect(url: str) -> Any:
            return conn

        listener = SettingsListener(
            "postgresql://listener.example.com/spark",
            reload,
            idle_timeout=0.05,
            connect=connect,
        )
        listener.start()
        deadline = time.monotonic() + BOUND_SECONDS
        while log.count("reload") < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await listener.stop()
        assert log == ["reload", "reload"]


# -- Two separate app instances on PostgreSQL ---------------------------------------------


class FakeSecretsManager:
    """One AWS Secrets Manager secret, shared by every replica's konfig client."""

    class exceptions:  # noqa: N801 - mirrors boto3
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self) -> None:
        self.secret = "{}"
        self.version_id = str(uuid.uuid4())
        self.lock = threading.Lock()

    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803 - boto3's name
        with self.lock:
            return {"SecretString": self.secret, "VersionId": self.version_id}

    def put_secret_value(
        self,
        SecretId: str,  # noqa: N803 - boto3's name
        SecretString: str,  # noqa: N803
        ClientRequestToken: str | None = None,  # noqa: N803
    ) -> dict[str, str]:
        with self.lock:
            self.secret = SecretString
            self.version_id = ClientRequestToken or str(uuid.uuid4())
            return {"VersionId": self.version_id}


def _encrypted_store(path: Path) -> Secrets:
    from konfig.secrets.encrypted_file import EncryptedFileBackend

    return Secrets(backend=EncryptedFileBackend(path, master_key=MASTER_KEY))


def _aws_store(client: FakeSecretsManager) -> Secrets:
    from konfig.secrets.aws_bundle_backend import AWSSecretsBundleBackend

    # The default five-minute read cache: only a refresh shows another replica's write.
    return Secrets(backend=AWSSecretsBundleBackend(arn=ARN, ttl=300, client=client))


class Ctx:
    def __init__(self, config: Path, secrets: Secrets) -> None:
        config.write_text(yaml.safe_dump({}), encoding="utf-8", newline="\n")
        self.settings = Settings(
            config_file=config,
            defaults={"interface": {"session_timeout_minutes": 60}},
        )
        self.secrets = secrets


def _client_options() -> dict[str, Any]:
    # psycopg's async driver needs the Selector loop on Windows; the portal's loop too.
    return {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


@contextmanager
def _server_app(
    tmp_path: Path, name: str, db: DatabaseSettings, secrets: Secrets
) -> Iterator[TestClient]:
    app = create_app(Ctx(tmp_path / f"{name}.yaml", secrets))  # type: ignore[arg-type]
    admin = identity_for(ADMINISTRATOR, user_guid=str(app.state.user_guid))
    app.add_middleware(AsIdentity, holder=Holder(admin))
    attach_database(app, db, True)
    with TestClient(app, backend_options=_client_options()) as client:
        sign_in(client)
        yield client


def _wait_for(predicate: Any, timeout: float = BOUND_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class KeyedSettings(ProviderSettings):
    """A test adapter whose settings carry a credential (a secret reference)."""

    kind: Literal["keyed"] = "keyed"
    api_key: SecretRef


class Built:
    """A conduit provider factory recording the key each build of the connection resolved."""

    kind = "keyed"
    settings_model = KeyedSettings

    def __init__(self) -> None:
        self.keys: list[str] = []

    def create_client(self, settings: Any, secrets: Any) -> Any:
        self.keys.append(secrets["api_key"].get_secret_value())
        return None

    def create_provider(
        self, name: str, settings: Any, client: Any, *, catalogue: Any = None
    ) -> Any:
        from conduit.providers.stub import StubFactory, StubSettings

        return StubFactory().create_provider(
            name, StubSettings(not_for_production=True), None, catalogue=catalogue
        )

    def last(self) -> str | None:
        return self.keys[-1] if self.keys else None


def _lab(client: TestClient) -> None:
    """Store the connection "lab", whose key is LAB_KEY in the shared store."""
    from spark.database import connections as repo

    sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
    sync_db.call(
        repo.save_connection,
        repo.ConnectionRow(
            id="lab",
            name="Lab",
            kind="keyed",
            settings={"kind": "keyed", "api_key": f"secret://{LAB_KEY}"},
            residency={"scopes": ["local"], "regions": None},
        ),
    )
    sync_db.call(
        repo.save_overlay,
        repo.OverlayRow(
            "keyed", "stub-echo", 8_192, "local", output_limit=1_024, connection_id="lab"
        ),
    )


def _save_lab_key(client: TestClient, value: str | None) -> None:
    """Save (or clear) the connection's key in the store and tell every replica."""
    store = client.app.state.ctx.secrets  # type: ignore[attr-defined]
    if value is None:
        store.delete(LAB_KEY)
    else:
        store.set(LAB_KEY, value)
    client.app.state.sync_db.call(announce, LAB_KEY)  # type: ignore[attr-defined]


def _wired(client: TestClient, built: Built) -> tuple[Any, Any]:
    """The client's connection runtime and conversation manager, following settings."""
    from conduit.integrations.konfig import KonfigSecretResolver

    from spark.connections import startup
    from spark.connections.runtime import ConnectionRuntime
    from spark.core.conversation_manager import ConversationManager

    app = client.app
    instance = server._server_consumers(app, app.state.ctx)  # type: ignore[arg-type, attr-defined]
    assert instance is not None
    runtime = ConnectionRuntime(
        secrets=KonfigSecretResolver(app.state.ctx.secrets),  # type: ignore[attr-defined]
        factories=[built],
    )
    load = startup.loader(app.state.sync_db, server=True, config_path=None)  # type: ignore[attr-defined]
    runtime.apply(load())
    app.state.connections = runtime  # type: ignore[attr-defined]  # closed with the app
    conv = ConversationManager(
        app.state.sync_db,  # type: ignore[attr-defined]
        runtime,
        embedded_tools_config=instance.embedded_tools_config(),
    )
    follower = instance.follow(startup.Refresher(runtime, load), conv)
    follower.wait_idle()
    app.state.settings_follower = follower  # type: ignore[attr-defined]
    return runtime, conv


def _web(conv: Any) -> dict[str, Any]:
    return dict(conv._embedded_tools_config["embedded_tools"]["web"])


def _save(client: TestClient, values: dict[str, Any]) -> None:
    resp = client.post("/settings/api/instance", json=values)
    assert resp.status_code == 200, resp.text


def _clear(client: TestClient, key: str) -> None:
    resp = client.post("/settings/api/secrets/clear", json={"key": key})
    assert resp.status_code == 200, resp.text


@pytest.fixture
def server_db(pg_url: str) -> DatabaseSettings:
    db = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    mode.configure(RuntimeSettings(Mode.SERVER, db))
    return db


@pytest.fixture
def built() -> Built:
    return Built()


@pytest.mark.postgres
@needs_konfig_reload
class TestSharedEncryptedFile:
    def test_a_secret_saved_on_one_instance_is_used_by_the_other_without_a_restart(
        self, tmp_path: Path, server_db: DatabaseSettings, built: Built
    ) -> None:
        shared = tmp_path / "secrets.enc"
        with ExitStack() as stack:
            a = stack.enter_context(_server_app(tmp_path, "a", server_db, _encrypted_store(shared)))
            b = stack.enter_context(_server_app(tmp_path, "b", server_db, _encrypted_store(shared)))
            assert a.app.state.ctx.secrets is not b.app.state.ctx.secrets  # type: ignore[attr-defined]
            _lab(a)
            runtime, conv = _wired(b, built)
            # The connection is known, but its key is not in the store yet.
            assert "lab" in runtime.failures() and built.last() is None

            # Only the secret changes: no settings row, so only its notification can tell B.
            _save_lab_key(a, "sk-ant-propagated-dev-only")
            started = time.monotonic()
            assert _wait_for(lambda: built.last() == "sk-ant-propagated-dev-only")
            assert time.monotonic() - started < BOUND_SECONDS
            assert _wait_for(lambda: "lab" not in runtime.failures())

            _save(a, {BRAVE: "brave-propagated-dev-only"})
            assert _wait_for(lambda: _web(conv).get("brave_api_key") == "brave-propagated-dev-only")

            # A changed key rebuilds the connection with it.
            _save_lab_key(a, "sk-ant-rotated-dev-only")
            assert _wait_for(lambda: built.last() == "sk-ant-rotated-dev-only")
            _clear(a, BRAVE)
            assert _wait_for(lambda: not _web(conv).get("brave_api_key"))

    def test_two_parallel_saves_of_different_secrets_both_survive(
        self, tmp_path: Path, server_db: DatabaseSettings
    ) -> None:
        from konfig.secrets.encrypted_file import EncryptedFileBackend

        shared = tmp_path / "secrets.enc"
        with ExitStack() as stack:
            a = stack.enter_context(_server_app(tmp_path, "a", server_db, _encrypted_store(shared)))
            b = stack.enter_context(_server_app(tmp_path, "b", server_db, _encrypted_store(shared)))
            for round_ in range(5):
                barrier = threading.Barrier(2)
                failures: list[BaseException] = []

                def save(client: TestClient, key: str, value: str) -> None:
                    try:
                        barrier.wait(BOUND_SECONDS)
                        _save(client, {key: value})
                    except BaseException as exc:  # noqa: BLE001 - reported below
                        failures.append(exc)

                first = f"sk-ant-round-{round_}-dev-only"
                second = f"xai-round-{round_}-dev-only"
                threads = [
                    threading.Thread(target=save, args=(a, ANTHROPIC, first)),
                    threading.Thread(target=save, args=(b, XAI, second)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(BOUND_SECONDS * 2)
                assert not failures, failures

                on_disk = EncryptedFileBackend(shared, master_key=MASTER_KEY)
                assert on_disk.get("voice_elevenlabs_api_key") == first
                assert on_disk.get("embedded_tools_web_bing_api_key") == second
                for client in (a, b):
                    status = client.get("/settings/api/secrets").json()
                    assert status[ANTHROPIC] is True and status[XAI] is True


@pytest.mark.postgres
@needs_konfig_reload
class TestSharedAwsBundle:
    def test_the_notification_refreshes_the_bundle_cache_on_the_other_instance(
        self, tmp_path: Path, server_db: DatabaseSettings, built: Built
    ) -> None:
        from spark.voice.engine import load_config

        manager = FakeSecretsManager()
        with ExitStack() as stack:
            a = stack.enter_context(_server_app(tmp_path, "a", server_db, _aws_store(manager)))
            b = stack.enter_context(_server_app(tmp_path, "b", server_db, _aws_store(manager)))
            _lab(a)
            _wired(b, built)
            b_ctx = b.app.state.ctx  # type: ignore[attr-defined]
            assert load_config(b_ctx).api_key == ""
            assert built.last() is None

            _save_lab_key(a, "sk-ant-bundle-dev-only")
            _save(a, {VOICE: "eleven-bundle-dev-only"})
            assert json.loads(manager.secret)[LAB_KEY] == "sk-ant-bundle-dev-only"
            assert _wait_for(lambda: built.last() == "sk-ant-bundle-dev-only")
            assert _wait_for(lambda: load_config(b_ctx).api_key == "eleven-bundle-dev-only")


# -- Never the value -------------------------------------------------------------------------


def _payloads_while(pg_url: str, action: Any) -> list[str]:
    """The settings notifications delivered while ``action`` runs."""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"LISTEN {CHANNEL}")
        action()
        payloads: list[str] = []
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            for notify in conn.notifies(timeout=0.2, stop_after=None):
                payloads.append(notify.payload)
        return payloads


def _rows_holding(pg_url: str, value: str) -> list[str]:
    """Every table in the database with a row whose text contains ``value``."""
    found: list[str] = []
    with psycopg.connect(pg_url, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' "
            "AND table_schema NOT IN ('pg_catalog', 'information_schema')"
        ).fetchall()
        for schema, table in tables:
            row = conn.execute(
                f'SELECT count(*) FROM "{schema}"."{table}" t WHERE t::text LIKE %s',
                (f"%{value}%",),
            ).fetchone()
            if row and row[0]:
                found.append(f"{schema}.{table}")
    return found


@pytest.mark.postgres
class TestNeverTheValue:
    def test_save_and_clear_notify_with_the_key_and_the_value_is_nowhere(
        self, tmp_path: Path, server_db: DatabaseSettings, pg_url: str
    ) -> None:
        value = "sk-ant-never-stored-dev-only"
        with _server_app(tmp_path, "a", server_db, _encrypted_store(tmp_path / "s.enc")) as a:
            saved = _payloads_while(pg_url, lambda: _save(a, {ANTHROPIC: value}))
            assert ANTHROPIC in saved
            assert not [p for p in saved if value in p]
            assert _rows_holding(pg_url, value) == []
            assert _rows_holding(pg_url, "never-stored") == []

            cleared = _payloads_while(pg_url, lambda: _clear(a, ANTHROPIC))
            assert cleared == [ANTHROPIC]


@pytest.mark.postgres
class TestListenerWiring:
    def test_the_lifespan_listener_refreshes_the_apps_secrets_store(
        self, tmp_path: Path, server_db: DatabaseSettings
    ) -> None:
        refreshed = threading.Event()

        class Watched(Secrets):
            def refresh(self) -> None:
                refreshed.set()

        from konfig.secrets.encrypted_file import EncryptedFileBackend

        store = Watched(backend=EncryptedFileBackend(tmp_path / "w.enc", master_key=MASTER_KEY))
        with _server_app(tmp_path, "a", server_db, store) as a:
            listener = a.app.state.settings_listener  # type: ignore[attr-defined]
            assert isinstance(listener, listener_mod.SettingsListener)
            refreshed.clear()
            _save(a, {ANTHROPIC: "sk-ant-wired-dev-only"})
            assert refreshed.wait(BOUND_SECONDS)
