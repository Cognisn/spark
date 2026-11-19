"""``spark bootstrap`` on PostgreSQL, with a local SMTP server (server mode).

Each test runs on a clone of the session's database at head, as its schema owner,
with restricted logins named uniquely for the test (logins are cluster-wide) and
the first administrator's invitation delivered to :class:`tests.fake_smtp.FakeSmtp`
over plain text on 127.0.0.1. It proves:

* a fresh first run sends one invitation, creates the logins with exactly their
  grants, creates the instance identity before web or worker starts, stores the
  start-up mail settings and records completion;
* a restart sends nothing and changes nothing; a changed login password is applied;
* two runs at once send one invitation;
* a delivery failure writes nothing, and a missing setting or a bad TLS setting
  refuses before anything is written;
* the worker waits for the identity and the record, then proceeds once they appear;
* the worker's own login cannot delete audit rows; the purger's can.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from warden.audit import set_recorder

from spark import mode
from spark.bootstrap import run as bootstrap_run
from spark.bootstrap import wait
from spark.bootstrap.run import BootstrapError
from spark.bootstrap.settings import BootstrapSettingsError
from spark.db import Database, migrate
from spark.identity import migrations as identity_migrations
from spark.identity.runtime import CSRF_KEY_SECRET
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.fake_smtp import FakeSmtp
from tests.test_identity_runtime import Konfig
from tests.test_instance_settings_web import MemorySecrets

pytestmark = pytest.mark.postgres

PUBLIC = "https://spark.example.com"
ADMIN = "first.admin@example.com"
SENDER = "spark@example.com"
WEB_PASSWORD = "web-bootstrap-dev-only"  # noqa: S105
WORKER_PASSWORD = "worker-bootstrap-dev-only"  # noqa: S105
PURGER_PASSWORD = "purger-bootstrap-dev-only"  # noqa: S105
TOTP_SECRET = "warden.totp-key"  # noqa: S105 - a secret's name


def _as_login(url: str, name: str, password: str) -> str:
    parts = urlsplit(url)
    netloc = f"{name}:{password}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


@dataclass
class Server:
    owner: DatabaseSettings
    names: dict[str, str]
    secrets: MemorySecrets

    @property
    def url(self) -> str:
        assert self.owner.url is not None
        return self.owner.url

    def login_url(self, which: str) -> str:
        key = f"database_url_{which}"
        value = self.secrets.get(key)
        assert value is not None
        return value


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    previous = set_recorder(None)
    identity_migrations.register()
    yield
    set_recorder(previous)
    migrate.clear_chains()
    mode.reset()


@pytest.fixture
def server(pg_server: str, _pg_template: str) -> Iterator[Server]:
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    owner_url = f"{base}/{name}"
    suffix = uuid.uuid4().hex[:8]
    names = {k: f"b{suffix}_{k}" for k in ("web", "worker", "purger")}
    passwords = {"web": WEB_PASSWORD, "worker": WORKER_PASSWORD, "purger": PURGER_PASSWORD}
    secrets = MemorySecrets(
        {f"database_url_{k}": _as_login(owner_url, n, passwords[k]) for k, n in names.items()}
    )
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url=owner_url))
    )
    try:
        yield Server(DatabaseSettings(Dialect.POSTGRESQL, url=owner_url), names, secrets)
    finally:
        with psycopg.connect(owner_url, autocommit=True) as conn:
            for login in names.values():
                exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,))
                if exists.fetchone():
                    conn.execute(f'DROP OWNED BY "{login}"')
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            for login in names.values():
                admin.execute(f'DROP ROLE IF EXISTS "{login}"')


@pytest.fixture
def smtp() -> Iterator[FakeSmtp]:
    with FakeSmtp("none") as server:
        yield server


def _settings(smtp_port: int, **extra: Any) -> Konfig:
    values: dict[str, Any] = {
        "server": {"public_base_url": PUBLIC},
        "warden": {"audit": {"retention_days": 365}},
        "bootstrap": {
            "admin": {"email": ADMIN, "first_name": "Ada", "last_name": "Example"},
            "database": {
                "web_url": "secret://database_url_web",
                "worker_url": "secret://database_url_worker",
            },
            "wait_seconds": 30,
            "wait_interval_seconds": 1,
        },
        "worker": {"audit_purge_database_url": "secret://database_url_purger"},
        "mail": {
            "smtp": {
                "host": "127.0.0.1",
                "port": smtp_port,
                "tls": "none",
                "sender_address": SENDER,
                "sender_name": "Spark",
            }
        },
    }
    for dotted, value in extra.items():
        node = values
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node.setdefault(part, {})
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return Konfig(values)


async def _run(server: Server, settings: Konfig, **kw: Any) -> bootstrap_run.Outcome:
    return await bootstrap_run.run(server.owner, settings, server.secrets, environ={}, **kw)


def _one(url: str, query: str, *params: Any) -> Any:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
    return None if row is None else row[0]


def _invited(smtp: FakeSmtp) -> list[str]:
    return [r for message in smtp.received for r in message.recipients]


async def _signing_key_works(server: Server, settings: Konfig, url: str) -> bool:
    db = await Database.open(DatabaseSettings(Dialect.POSTGRESQL, url=url), max_size=2)
    try:
        return not await wait.missing(db, settings, server.secrets, record=True)
    finally:
        await db.close()


class TestFirstRun:
    async def test_a_fresh_first_run(self, server: Server, smtp: FakeSmtp) -> None:
        settings = _settings(smtp.port)
        outcome = await _run(server, settings)
        assert outcome.invited and not outcome.already_complete
        assert set(outcome.logins.values()) == {"created"}
        # One invitation, to the first administrator, with their name.
        assert _invited(smtp) == [ADMIN]
        assert ADMIN in str(smtp.last()["To"])
        # The record, the staged role, the identity.
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 1
        assert (
            _one(
                server.url,
                "SELECT count(*) FROM warden.invitation_grants g JOIN warden.users u"
                " ON u.id = g.user_id WHERE u.email = %s AND g.role = 'administrator'",
                ADMIN,
            )
            == 1
        )
        assert server.secrets.get(TOTP_SECRET)
        assert server.secrets.get(CSRF_KEY_SECRET)
        # Before any web or worker: the worker's own login sees nothing missing.
        assert await _signing_key_works(server, settings, server.login_url("worker"))
        # The start-up mail settings were stored for the instance page.
        assert (
            _one(
                server.url,
                "SELECT value FROM spark.instance_settings WHERE key = %s",
                "mail.smtp.host",
            )
            == '"127.0.0.1"'
        )

    async def test_the_logins_work_with_exactly_their_grants(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        await _run(server, _settings(smtp.port))
        for which in ("web", "worker"):
            url = server.login_url(which)
            migrate.check_at_head(DatabaseSettings(Dialect.POSTGRESQL, url=url))
            with psycopg.connect(url, autocommit=True) as conn:
                assert conn.execute("SELECT count(*) FROM spark.bootstrap").fetchone() == (1,)
                conn.execute("SELECT count(*) FROM warden.audit_events")
                for statement in (
                    "DELETE FROM warden.audit_events WHERE false",
                    "UPDATE warden.audit_events SET action = action WHERE false",
                    "INSERT INTO spark.alembic_version VALUES ('x')",
                    "CREATE SCHEMA nope",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute(statement)
        purger = server.login_url("purger")
        with psycopg.connect(purger, autocommit=True) as conn:
            conn.execute("DELETE FROM warden.audit_events WHERE false")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM spark.instance_settings")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE warden.audit_events SET action = action WHERE false")

    async def test_without_restricted_logins_none_are_made(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(
            smtp.port,
            bootstrap__database__web_url=None,
            bootstrap__database__worker_url=None,
            warden__audit__retention_days=None,
        )
        outcome = await _run(server, settings)
        assert outcome.logins == {} and outcome.invited
        assert (
            _one(
                server.url, "SELECT count(*) FROM pg_roles WHERE rolname = %s", server.names["web"]
            )
            == 0
        )


class TestRestart:
    async def test_a_restart_sends_nothing_and_changes_nothing(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port)
        await _run(server, settings)
        users = _one(server.url, "SELECT count(*) FROM warden.users")
        keys = _one(server.url, "SELECT count(*) FROM warden.signing_keys")
        totp = server.secrets.get(TOTP_SECRET)
        again = await _run(server, settings)
        assert again.already_complete and not again.invited
        assert set(again.logins.values()) == {"unchanged"}
        assert again.mail_seeded == ()
        assert _invited(smtp) == [ADMIN]
        assert _one(server.url, "SELECT count(*) FROM warden.users") == users
        assert _one(server.url, "SELECT count(*) FROM warden.signing_keys") == keys
        assert server.secrets.get(TOTP_SECRET) == totp

    async def test_a_changed_password_is_applied(self, server: Server, smtp: FakeSmtp) -> None:
        settings = _settings(smtp.port)
        await _run(server, settings)
        changed = _as_login(server.url, server.names["web"], "web-changed-dev-only")
        server.secrets.set("database_url_web", changed)
        again = await _run(server, settings)
        assert again.logins[server.names["web"]] == "password changed"
        with psycopg.connect(changed, autocommit=True) as conn:
            assert conn.execute("SELECT current_user").fetchone() == (server.names["web"],)

    async def test_an_existing_administrator_is_not_invited_again(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port)
        await _run(server, settings)
        with psycopg.connect(server.url, autocommit=True) as conn:
            conn.execute("DELETE FROM spark.bootstrap")
        again = await _run(server, settings)
        assert not again.invited and not again.already_complete
        assert _invited(smtp) == [ADMIN]
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 1

    async def test_resend_on_request(self, server: Server, smtp: FakeSmtp) -> None:
        settings = _settings(smtp.port)
        await _run(server, settings)
        again = await _run(server, settings, resend_invitation=True)
        assert again.resent and not again.invited
        assert _invited(smtp) == [ADMIN, ADMIN]


class TestConcurrency:
    async def test_two_runs_at_once_send_one_invitation(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port)
        first, second = await asyncio.gather(_run(server, settings), _run(server, settings))
        assert sorted([first.invited, second.invited]) == [False, True]
        assert _invited(smtp) == [ADMIN]
        assert _one(server.url, "SELECT count(*) FROM warden.users") == 1
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 1


class TestRefusals:
    async def test_a_delivery_failure_writes_nothing(self, server: Server) -> None:
        with FakeSmtp("none") as closed:
            port = closed.port
        with pytest.raises(BootstrapError, match="could not be sent; nothing was written"):
            await _run(server, _settings(port))
        assert _one(server.url, "SELECT count(*) FROM warden.users") == 0
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 0

    async def test_a_missing_setting_refuses_before_writing(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port, bootstrap__admin__last_name=None)
        with pytest.raises(BootstrapSettingsError, match="SPARK__BOOTSTRAP__ADMIN__LAST_NAME"):
            await _run(server, settings)
        assert (
            _one(
                server.url, "SELECT count(*) FROM pg_roles WHERE rolname = %s", server.names["web"]
            )
            == 0
        )
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 0
        assert smtp.received == []

    async def test_tls_asked_of_a_server_without_it(self, server: Server, smtp: FakeSmtp) -> None:
        owner = DatabaseSettings(Dialect.POSTGRESQL, url=server.url + "?sslmode=require")
        with pytest.raises(BootstrapError) as caught:
            await bootstrap_run.run(owner, _settings(smtp.port), server.secrets, environ={})
        text = str(caught.value)
        assert text.startswith("database.url (SPARK__DATABASE__URL):")
        assert "spark-test-dev-only" not in text and "@" not in text
        assert smtp.received == []

    async def test_verify_full_without_a_root_certificate(
        self, server: Server, smtp: FakeSmtp, tmp_path: Any
    ) -> None:
        owner = DatabaseSettings(Dialect.POSTGRESQL, url=server.url + "?sslmode=verify-full")
        with pytest.raises(BootstrapSettingsError, match="no readable root certificate"):
            await bootstrap_run.run(
                owner, _settings(smtp.port), server.secrets, environ={"HOME": str(tmp_path)}
            )


class TestWorkerWait:
    async def test_the_worker_waits_then_proceeds_once_bootstrap_has_run(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port)
        sleeps: list[float] = []

        async def bootstrap_meanwhile(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                await _run(server, settings)

        await wait.before_worker(server.owner, settings, server.secrets, sleep=bootstrap_meanwhile)
        assert sleeps == [1.0]
        assert _invited(smtp) == [ADMIN]

    async def test_the_worker_gives_up_naming_what_is_missing(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port, bootstrap__wait_seconds=2)
        now = [0.0]

        async def tick(seconds: float) -> None:
            now[0] += seconds

        with pytest.raises(wait.BootstrapNotReady) as caught:
            await wait.before_worker(
                server.owner, settings, server.secrets, sleep=tick, clock=lambda: now[0]
            )
        text = str(caught.value)
        assert wait.RECORD in text and TOTP_SECRET in text and CSRF_KEY_SECRET in text
        assert wait.SIGNING_KEY in text

    async def test_web_waits_for_the_record_when_bootstrap_is_configured(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(
            smtp.port, bootstrap__wait_seconds=0.5, bootstrap__wait_interval_seconds=0.1
        )
        db = await Database.open(server.owner, max_size=2)
        try:
            with pytest.raises(wait.BootstrapNotReady, match="spark web"):
                await wait.before_web(db, settings, server.secrets)
            await _run(server, settings)
            await wait.before_web(db, settings, server.secrets)
        finally:
            await db.close()
