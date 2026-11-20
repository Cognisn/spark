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


class CachedStore:
    """A secrets store as konfig's AWS bundle behaves: a write-through cache over a store.

    ``get`` answers from the cache, ``refresh`` re-reads the store, and ``on_set``
    may make the store lose a write that the cache keeps (the eventually
    consistent read that rc22 hit).
    """

    def __init__(self, on_set: Any = None) -> None:
        self.store: dict[str, str] = {"original.entry": "synthetic-original"}
        self.cache: dict[str, str] = dict(self.store)
        self.baseline = dict(self.store)
        self.on_set = on_set

    def get(self, key: str) -> str | None:
        return self.cache.get(key)

    def set(self, key: str, value: str) -> None:
        self.cache[key] = value
        if self.on_set is None or self.on_set(self, key, value):
            self.store[key] = value

    def refresh(self) -> None:
        self.cache = dict(self.store)

    def list_keys(self) -> list[str]:
        return list(self.cache)


def _lose_name(name_start: str) -> Any:
    """A store that never keeps a write to a name beginning ``name_start``."""
    return lambda store, key, value: not key.startswith(name_start)


def _stale_signing_write(store: CachedStore, key: str, value: str) -> bool:
    """The rc22 sequence: the signing key's write is based on the bundle from before the others."""
    if key.startswith("warden.signing-key."):
        store.store = {**store.baseline, key: value}
        return False
    return True


def _stale_signing_write_without_csrf(store: CachedStore, key: str, value: str) -> bool:
    """The signing key's write lands on a bundle that has the TOTP key but not the CSRF key."""
    if key.startswith("warden.signing-key."):
        store.store = {k: v for k, v in store.store.items() if k != CSRF_KEY_SECRET}
        store.store[key] = value
        return False
    return True


@pytest.fixture
def no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building Identity (warden's SecretBox) must never be reached when a key is missing."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Identity was built although an identity key was not kept")

    monkeypatch.setattr(runtime_mod, "Identity", refuse)


class TestEnsureSecret:
    def test_a_write_the_store_dropped_is_not_read_back_from_the_cache(self) -> None:
        store = CachedStore(_lose_name("synthetic.name"))
        with pytest.raises(StartupConfigError, match="did not keep synthetic.name"):
            runtime_mod._ensure_secret(store, "synthetic.name", lambda: "synthetic-value")

    def test_a_kept_write_is_returned(self) -> None:
        store = CachedStore()
        value = runtime_mod._ensure_secret(store, "synthetic.name", lambda: "synthetic-value")
        assert value == "synthetic-value" and store.store["synthetic.name"] == value


@pytest.mark.postgres
class TestStoreVerification:
    async def test_the_rc22_sequence_fails_naming_the_lost_secret(
        self, database: Any, no_identity: None
    ) -> None:
        store = CachedStore(_stale_signing_write)
        with pytest.raises(StartupConfigError, match="did not keep warden.totp-key") as caught:
            await IdentityRuntime.start(database, _settings(), store)
        assert "synthetic-original" not in str(caught.value)  # names only, never values
        assert "warden.totp-key" not in store.store

    async def test_a_csrf_key_lost_after_its_own_check_is_caught_at_the_end(
        self, database: Any, no_identity: None
    ) -> None:
        store = CachedStore(_stale_signing_write_without_csrf)
        with pytest.raises(StartupConfigError, match=f"did not keep {CSRF_KEY_SECRET}"):
            await IdentityRuntime.start(database, _settings(), store)

    @pytest.mark.parametrize("lost", ["warden.totp-key", CSRF_KEY_SECRET, "warden.signing-key."])
    async def test_any_one_lost_key_fails_by_name(
        self, database: Any, no_identity: None, lost: str
    ) -> None:
        store = CachedStore(_lose_name(lost))
        with pytest.raises(StartupConfigError, match=f"did not keep {lost}"):
            await IdentityRuntime.start(database, _settings(), store)

    async def test_a_rerun_against_the_rc22_state_keeps_the_key_and_adds_the_rest(
        self, database: Any
    ) -> None:
        secrets = MemorySecrets({"original.entry": "synthetic-original"})
        first = await IdentityRuntime.start(database, _settings(), secrets)
        first.close()
        # What rc22 left: the original entries and a signing key, no TOTP or CSRF key.
        secrets.delete("warden.totp-key")
        secrets.delete(CSRF_KEY_SECRET)
        seeds = {k: v for k, v in secrets.data.items() if k.startswith("warden.signing-key.")}
        assert len(seeds) == 1
        second = await IdentityRuntime.start(database, _settings(), secrets)
        try:
            assert secrets.get("warden.totp-key") and secrets.get(CSRF_KEY_SECRET)
            assert secrets.get("original.entry") == "synthetic-original"
            # The signing key was usable, so it is kept: no second key, same seed.
            assert await _key_count(database) == 1
            assert {
                k: v for k, v in secrets.data.items() if k.startswith("warden.signing-key.")
            } == seeds
        finally:
            second.close()

    async def test_a_signing_key_without_its_seed_is_rotated(self, database: Any) -> None:
        secrets = MemorySecrets()
        first = await IdentityRuntime.start(database, _settings(), secrets)
        first.close()
        for name in [k for k in secrets.data if k.startswith("warden.signing-key.")]:
            secrets.delete(name)
        second = await IdentityRuntime.start(database, _settings(), secrets)
        try:
            assert await _key_count(database) == 2
            active = await second.keys.active_key()
            assert secrets.get(second.config.keys.secret_prefix + active.key_id)
        finally:
            second.close()


def test_module_names_its_public_api() -> None:
    assert "IdentityRuntime" in runtime_mod.__all__
