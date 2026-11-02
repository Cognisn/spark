"""warden's runtime inside SPARK: its configuration, keys, secrets and audit recorder.

The configuration is derived from SPARK's settings: one public address
(server.public_base_url), SPARK's own prefixes and link templates, and the rest
from the ``warden`` section. At start-up a missing signing key is rotated in
and missing secrets (the TOTP key, the CSRF key) are generated into konfig's
secrets store; the audit recorder is installed before Identity and restored
when the runtime closes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from warden.audit import get_recorder, set_recorder

from spark.identity import runtime as runtime_mod
from spark.identity.runtime import (
    ACCEPT_INVITATION_LINK,
    CSRF_KEY_SECRET,
    RESET_PASSWORD_LINK,
    IdentityRuntime,
    warden_config,
)
from spark.instance_settings.startup import StartupConfigError
from spark.mode import DatabaseSettings, Dialect
from tests.test_instance_settings_web import MemorySecrets

PUBLIC = "https://spark.example.com"


class Konfig:
    """konfig's settings: dotted ``get`` and nested ``get_section``."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self.values
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def get_section(self, prefix: str) -> dict[str, Any]:
        found = self.get(prefix, {})
        return dict(found) if isinstance(found, dict) else {}


def _settings(**warden: Any) -> Konfig:
    return Konfig({"server": {"public_base_url": PUBLIC}, "warden": warden})


class TestConfig:
    def test_http_is_derived_from_the_public_address(self) -> None:
        config = warden_config(_settings(), MemorySecrets(), PUBLIC)
        assert config.http.public_base_url == PUBLIC
        assert config.http.accept_invitation_link == ACCEPT_INVITATION_LINK
        assert config.http.reset_password_link == RESET_PASSWORD_LINK
        assert config.http.render_link(config.http.reset_password_link, token="t") == (
            f"{PUBLIC}/auth/reset?token=t"
        )
        # warden's own routers, if mounted, sit under SPARK's identity prefix.
        for prefix in (
            config.http.auth_prefix,
            config.http.admin_prefix,
            config.http.audit_prefix,
            config.http.keys_prefix,
        ):
            assert prefix.startswith("/identity/")

    def test_the_rest_comes_from_the_warden_section(self) -> None:
        config = warden_config(
            _settings(identity={"require_mfa": False, "access_token_ttl_seconds": 600}),
            MemorySecrets(),
            PUBLIC,
        )
        assert config.identity.require_mfa is False
        assert config.identity.access_token_ttl_seconds == 600

    def test_secret_references_resolve_through_konfig(self) -> None:
        config = warden_config(
            _settings(mail={"from_address": "secret://mail_from"}),
            MemorySecrets({"mail_from": "spark@example.com"}),
            PUBLIC,
        )
        assert config.mail.from_address == "spark@example.com"

    def test_a_second_public_address_is_refused(self) -> None:
        with pytest.raises(StartupConfigError, match="warden.http"):
            warden_config(
                _settings(http={"public_base_url": "https://other.example.com"}),
                MemorySecrets(),
                PUBLIC,
            )

    @pytest.mark.parametrize("key", ["url", "client_id", "client_secret"])
    def test_the_retired_placeholders_are_refused(self, key: str) -> None:
        with pytest.raises(StartupConfigError, match=f"warden.{key}"):
            warden_config(_settings(**{key: "x"}), MemorySecrets(), PUBLIC)

    def test_an_invalid_value_names_the_setting(self) -> None:
        with pytest.raises(StartupConfigError, match="warden.identity.access_token_ttl_seconds"):
            warden_config(
                _settings(identity={"access_token_ttl_seconds": 5}), MemorySecrets(), PUBLIC
            )


# -- Start-up against PostgreSQL ------------------------------------------------------------


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
async def database(server_db: DatabaseSettings) -> AsyncIterator[Any]:
    from spark.db import Database

    db = await Database.open(server_db)
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture(autouse=True)
def _restore_recorder() -> Iterator[None]:
    previous = set_recorder(None)
    yield
    set_recorder(previous)


async def _key_count(database: Any) -> int:
    value = await database.fetch_value("SELECT count(*) FROM warden.signing_keys")
    return int(value)


@pytest.mark.postgres
class TestStart:
    async def test_first_start_creates_the_key_and_secrets(self, database: Any) -> None:
        secrets = MemorySecrets()
        assert await _key_count(database) == 0
        runtime = await IdentityRuntime.start(database, _settings(), secrets)
        try:
            assert await _key_count(database) == 1
            config = runtime.config
            assert secrets.get(config.identity.totp_key_secret)
            assert secrets.get(CSRF_KEY_SECRET)
            assert any(k.startswith(config.keys.secret_prefix) for k in secrets.list_keys())
            assert len(runtime.csrf_key) >= 32
        finally:
            runtime.close()

    async def test_a_second_start_reuses_them(self, database: Any) -> None:
        secrets = MemorySecrets()
        first = await IdentityRuntime.start(database, _settings(), secrets)
        first.close()
        saved = dict(secrets.data)
        second = await IdentityRuntime.start(database, _settings(), secrets)
        second.close()
        assert await _key_count(database) == 1
        assert secrets.data == saved
        assert second.csrf_key == first.csrf_key

    async def test_concurrent_starts_agree_on_one_key_and_one_totp_key(self, database: Any) -> None:
        secrets = MemorySecrets()
        runtimes = await asyncio.gather(
            *(IdentityRuntime.start(database, _settings(), secrets) for _ in range(3))
        )
        for runtime in runtimes:
            runtime.close()
        assert await _key_count(database) == 1
        assert len({r.csrf_key for r in runtimes}) == 1

    async def test_the_recorder_is_installed_and_restored(self, database: Any) -> None:
        runtime = await IdentityRuntime.start(database, _settings(), MemorySecrets())
        assert get_recorder() is runtime.recorder
        runtime.close()
        with pytest.raises(RuntimeError):
            get_recorder()

    async def test_identity_signs_in_with_the_derived_config(self, database: Any) -> None:
        runtime = await IdentityRuntime.start(
            database, _settings(identity={"require_mfa": False}), MemorySecrets()
        )
        try:
            identity = runtime.identity
            await identity.users.create(
                "ada@example.com", actor="test", password="correct-horse-battery-dev-only"
            )
            result = await identity.sign_in("ada@example.com", "correct-horse-battery-dev-only")
            assert result.tokens is not None
            principal = await identity.authenticate(result.tokens.access_token)
            assert principal.email == "ada@example.com"
        finally:
            runtime.close()

    async def test_running_wraps_identity_and_closes(self, database: Any) -> None:
        runtime = await IdentityRuntime.start(database, _settings(), MemorySecrets())
        async with runtime.running():
            assert get_recorder() is runtime.recorder
        with pytest.raises(RuntimeError):
            get_recorder()

    async def test_sqlite_is_refused(self, tmp_path: Any) -> None:
        from spark.db import Database
        from spark.db.engine import DatabaseError

        db = await Database.open(DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db"))
        try:
            with pytest.raises(DatabaseError):
                await IdentityRuntime.start(db, _settings(), MemorySecrets())
        finally:
            await db.close()


def test_module_names_its_public_api() -> None:
    assert "IdentityRuntime" in runtime_mod.__all__
