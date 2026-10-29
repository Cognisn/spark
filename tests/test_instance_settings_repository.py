"""The instance settings and user preferences repository, on SQLite and PostgreSQL."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spark.db import migrate
from spark.instance_settings import repository
from spark.instance_settings.registry import (
    InvalidSettingError,
    SettingsError,
    UnknownSettingError,
)
from spark.mode import Dialect

SECRET = "sk-ant-repository-test-dev-only"


class TestInstanceSettings:
    async def test_empty_by_default(self, spark_db: Any) -> None:
        assert await repository.load_instance(spark_db) == {}

    async def test_save_and_load_round_trip(self, spark_db: Any) -> None:
        await repository.save_instance(spark_db, "providers.anthropic.enabled", True, actor="a")
        await repository.save_instance(spark_db, "models.offered", ["m-1", "m-2"], actor="a")
        await repository.save_instance(
            spark_db, "tool_policy.actions", {"web": "withhold"}, actor="a"
        )
        loaded = await repository.load_instance(spark_db)
        assert loaded == {
            "providers.anthropic.enabled": True,
            "models.offered": ["m-1", "m-2"],
            "tool_policy.actions": {"web": "withhold"},
        }

    async def test_save_coerces_form_values(self, spark_db: Any) -> None:
        await repository.save_instance(
            spark_db, "voice.elevenlabs.monthly_character_cap", "500", actor="a"
        )
        loaded = await repository.load_instance(spark_db)
        assert loaded["voice.elevenlabs.monthly_character_cap"] == 500

    async def test_save_overwrites(self, spark_db: Any) -> None:
        await repository.save_instance(spark_db, "voice.engine", "elevenlabs", actor="a")
        await repository.save_instance(spark_db, "voice.engine", "browser", actor="b")
        assert (await repository.load_instance(spark_db))["voice.engine"] == "browser"
        actor = await spark_db.fetch_value(
            "SELECT updated_by FROM instance_settings WHERE key = ?", ("voice.engine",)
        )
        assert actor == "b"

    async def test_delete_restores_the_default(self, spark_db: Any) -> None:
        await repository.save_instance(spark_db, "voice.engine", "elevenlabs", actor="a")
        await repository.delete_instance(spark_db, "voice.engine", actor="a")
        assert await repository.load_instance(spark_db) == {}

    async def test_unknown_key_refused(self, spark_db: Any) -> None:
        with pytest.raises(UnknownSettingError):
            await repository.save_instance(spark_db, "no.such.key", 1, actor="a")
        assert await repository.load_instance(spark_db) == {}

    async def test_invalid_value_refused(self, spark_db: Any) -> None:
        with pytest.raises(InvalidSettingError):
            await repository.save_instance(spark_db, "voice.engine", "telegraph", actor="a")

    async def test_user_and_startup_keys_refused(self, spark_db: Any) -> None:
        for key, value in (("interface.theme", "dark"), ("mode", "server")):
            with pytest.raises(SettingsError):
                await repository.save_instance(spark_db, key, value, actor="a")
        assert await repository.load_instance(spark_db) == {}

    async def test_secret_keys_are_never_written(self, spark_db: Any) -> None:
        with pytest.raises(SettingsError):
            await repository.save_instance(
                spark_db, "providers.anthropic.api_key", SECRET, actor="a"
            )
        count = await spark_db.fetch_value("SELECT COUNT(*) FROM instance_settings")
        assert count == 0

    async def test_rows_with_unknown_or_invalid_values_are_skipped_on_load(
        self, spark_db: Any
    ) -> None:
        await spark_db.execute(
            "INSERT INTO instance_settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?)",
            ("retired.key", "true", "2026-10-29T00:00:00+00:00", "a"),
        )
        await spark_db.execute(
            "INSERT INTO instance_settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?)",
            ("voice.engine", '"telegraph"', "2026-10-29T00:00:00+00:00", "a"),
        )
        assert await repository.load_instance(spark_db) == {}


class TestPreferences:
    async def test_round_trip_is_per_user(self, spark_db: Any) -> None:
        await repository.save_preference(spark_db, "user-a", "interface.theme", "dark")
        await repository.save_preference(spark_db, "user-b", "interface.theme", "light")
        await repository.save_preference(
            spark_db, "user-a", "conversation.global_instructions", "Be brief."
        )
        assert await repository.load_preferences(spark_db, "user-a") == {
            "interface.theme": "dark",
            "conversation.global_instructions": "Be brief.",
        }
        assert await repository.load_preferences(spark_db, "user-b") == {"interface.theme": "light"}

    async def test_delete(self, spark_db: Any) -> None:
        await repository.save_preference(spark_db, "user-a", "interface.theme", "dark")
        await repository.delete_preference(spark_db, "user-a", "interface.theme")
        assert await repository.load_preferences(spark_db, "user-a") == {}

    async def test_only_user_scope_keys(self, spark_db: Any) -> None:
        for key, value in (
            ("providers.anthropic.enabled", True),
            ("providers.anthropic.api_key", SECRET),
            ("no.such.key", 1),
        ):
            with pytest.raises(SettingsError):
                await repository.save_preference(spark_db, "user-a", key, value)
        assert await repository.load_preferences(spark_db, "user-a") == {}

    async def test_user_guid_required(self, spark_db: Any) -> None:
        with pytest.raises(ValueError):
            await repository.save_preference(spark_db, "", "interface.theme", "dark")


@pytest.fixture
def pg_settings(pg_url: str) -> Any:
    """A fresh PostgreSQL database at head (server mode only)."""
    from spark.mode import DatabaseSettings

    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    migrate.upgrade(settings)
    return settings


@pytest.fixture
async def pg_db(pg_settings: Any) -> Any:
    from spark.db import Database

    db = await Database.open(pg_settings)
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.postgres
class TestNotify:
    async def test_save_notifies_listeners_on_commit(self, pg_db: Any, pg_url: str) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as listener:
            await listener.execute(f"LISTEN {repository.CHANNEL}")
            await repository.save_instance(pg_db, "voice.engine", "elevenlabs", actor="a")
            received = await _next_notify(listener)
        assert received == "voice.engine"

    async def test_delete_notifies(self, pg_db: Any, pg_url: str) -> None:
        import psycopg

        await repository.save_instance(pg_db, "voice.engine", "elevenlabs", actor="a")
        async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as listener:
            await listener.execute(f"LISTEN {repository.CHANNEL}")
            await repository.delete_instance(pg_db, "voice.engine", actor="a")
            assert await _next_notify(listener) == "voice.engine"

    async def test_refused_write_sends_nothing(self, pg_db: Any, pg_url: str) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as listener:
            await listener.execute(f"LISTEN {repository.CHANNEL}")
            with pytest.raises(SettingsError):
                await repository.save_instance(pg_db, "voice.engine", "telegraph", actor="a")
            assert await _next_notify(listener, timeout=0.5) is None


async def _next_notify(conn: Any, timeout: float = 5.0) -> str | None:
    async def _first() -> str | None:
        async for notify in conn.notifies(timeout=timeout, stop_after=1):
            return str(notify.payload)
        return None

    return await asyncio.wait_for(_first(), timeout + 1)


class TestMigration:
    def test_tables_exist_at_head(self, spark_db_settings: Any) -> None:
        tables = _tables(spark_db_settings)
        assert {"instance_settings", "user_preferences"} <= tables

    def test_downgrade_removes_the_tables_and_upgrade_restores_them(
        self, spark_db_settings: Any
    ) -> None:
        migrate.downgrade(spark_db_settings, migrate.BASELINE)
        assert migrate.current(spark_db_settings) == migrate.BASELINE
        assert not {"instance_settings", "user_preferences"} & _tables(spark_db_settings)
        migrate.upgrade(spark_db_settings)
        assert migrate.current(spark_db_settings) == migrate.head()
        assert {"instance_settings", "user_preferences"} <= _tables(spark_db_settings)

    def test_revision_is_on_the_chain_after_the_baseline(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(migrate._SCRIPTS))
        chain = [rev.revision for rev in ScriptDirectory.from_config(cfg).walk_revisions()]
        assert repository.REVISION in chain
        assert chain.index(repository.REVISION) < chain.index(migrate.BASELINE)


def _tables(settings: Any) -> set[str]:
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.pool import NullPool

    if settings.dialect is Dialect.SQLITE:
        engine = create_engine(f"sqlite:///{settings.sqlite_path}", poolclass=NullPool)
        schema = None
    else:
        _, rest = settings.url.split("://", 1)
        engine = create_engine(f"postgresql+psycopg://{rest}", poolclass=NullPool)
        schema = "spark"
    try:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names(schema=schema))
    finally:
        engine.dispose()
