"""The one accessor for settings in either mode, the audit hook and the access check stub."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.instance_settings import access, accessor, audit, repository
from spark.instance_settings.accessor import FileSettings, ServerSettings
from spark.instance_settings.audit import SettingChange
from spark.instance_settings.registry import SettingsError, UnknownSettingError
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings


class FakeKonfig:
    """The part of konfig's Settings the accessor reads."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    accessor.reset()
    audit.set_audit_hook(None)
    access.reset()
    mode.reset()


class TestFileSettings:
    def test_reads_the_config_value(self) -> None:
        source = FileSettings(FakeKonfig({"voice.engine": "elevenlabs"}))
        assert source.get("voice.engine") == "elevenlabs"

    def test_falls_back_to_the_registry_default(self) -> None:
        assert FileSettings(FakeKonfig({})).get("conversation.max_tool_iterations") == 25

    def test_coerces_config_forms(self) -> None:
        source = FileSettings(FakeKonfig({"embedded_tools.email.use_tls": "false"}))
        assert source.get("embedded_tools.email.use_tls") is False

    def test_invalid_config_value_falls_back_to_the_default(self) -> None:
        source = FileSettings(FakeKonfig({"voice.engine": "telegraph"}))
        assert source.get("voice.engine") == "browser"

    def test_unknown_key_refused(self) -> None:
        with pytest.raises(UnknownSettingError):
            FileSettings(FakeKonfig({})).get("not.a.key")

    def test_secret_keys_are_not_read_through_the_accessor(self) -> None:
        source = FileSettings(FakeKonfig({"providers.anthropic.api_key": "secret://x"}))
        with pytest.raises(SettingsError):
            source.get("providers.anthropic.api_key")


class TestServerSettings:
    async def test_defaults_before_anything_is_stored(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({}))
        await source.reload()
        assert source.get("voice.engine") == "browser"

    async def test_reads_instance_values_from_the_database_not_the_config(
        self, spark_db: Any
    ) -> None:
        await repository.save_instance(spark_db, "voice.engine", "elevenlabs", actor="a")
        source = ServerSettings(spark_db, FakeKonfig({"voice.engine": "browser"}))
        await source.reload()
        assert source.get("voice.engine") == "elevenlabs"

    async def test_startup_keys_come_from_konfig(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({"logging.level": "DEBUG"}))
        await source.reload()
        assert source.get("logging.level") == "DEBUG"

    async def test_set_writes_through_and_updates_the_cache(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({}))
        await source.reload()
        assert await source.set("voice.engine", "elevenlabs", actor="admin") == "elevenlabs"
        assert source.get("voice.engine") == "elevenlabs"
        assert (await repository.load_instance(spark_db))["voice.engine"] == "elevenlabs"

    async def test_clear_reverts_to_the_default(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({}))
        await source.set("voice.engine", "elevenlabs", actor="admin")
        await source.clear("voice.engine", actor="admin")
        assert source.get("voice.engine") == "browser"

    async def test_reload_sees_another_writer(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({}))
        await source.reload()
        await repository.save_instance(spark_db, "models.offered", ["m-1"], actor="other")
        assert source.get("models.offered") == []
        await source.reload()
        assert source.get("models.offered") == ["m-1"]

    async def test_observers_hear_every_change(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({}))
        seen: list[int] = []
        source.on_change(lambda: seen.append(source.generation))
        await source.reload()
        await source.set("voice.engine", "elevenlabs", actor="admin")
        assert seen == [1, 2]

    async def test_secret_and_user_keys_refused_by_set(self, spark_db: Any) -> None:
        source = ServerSettings(spark_db, FakeKonfig({}))
        for key, value in (
            ("providers.anthropic.api_key", "sk-accessor-dev-only"),
            ("interface.theme", "dark"),
            ("mode", "personal"),
        ):
            with pytest.raises(SettingsError):
                await source.set(key, value, actor="admin")

    async def test_set_records_an_audit_event_without_the_value(self, spark_db: Any) -> None:
        events: list[SettingChange] = []
        audit.set_audit_hook(events.append)
        source = ServerSettings(spark_db, FakeKonfig({}))
        await source.set("voice.engine", "elevenlabs", actor="admin-1")
        await source.clear("voice.engine", actor="admin-1")
        assert [(e.key, e.action, e.actor, e.secret) for e in events] == [
            ("voice.engine", "set", "admin-1", False),
            ("voice.engine", "clear", "admin-1", False),
        ]
        assert not hasattr(events[0], "value")

    async def test_refused_set_records_nothing(self, spark_db: Any) -> None:
        events: list[SettingChange] = []
        audit.set_audit_hook(events.append)
        source = ServerSettings(spark_db, FakeKonfig({}))
        with pytest.raises(SettingsError):
            await source.set("voice.engine", "telegraph", actor="admin")
        assert events == []


class TestModuleAccessor:
    def test_get_before_configure_raises(self) -> None:
        with pytest.raises(RuntimeError):
            accessor.get("voice.engine")

    def test_get_uses_the_configured_source(self) -> None:
        accessor.configure(FileSettings(FakeKonfig({"voice.engine": "elevenlabs"})))
        assert accessor.get("voice.engine") == "elevenlabs"
        assert isinstance(accessor.current(), FileSettings)

    async def test_preferences_in_personal_mode_come_from_the_config(self) -> None:
        mode.configure(
            RuntimeSettings(Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=None))
        )
        accessor.configure(FileSettings(FakeKonfig({"interface.theme": "light"})))
        prefs = await accessor.preferences(None, "local-user")
        assert prefs["interface.theme"] == "light"
        assert prefs["voice.interaction_mode"] == "listen_along"

    async def test_preferences_in_server_mode_are_per_user(self, spark_db: Any) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
        await repository.save_preference(spark_db, "user-a", "interface.theme", "dark")
        a = await accessor.preferences(spark_db, "user-a")
        b = await accessor.preferences(spark_db, "user-b")
        assert a["interface.theme"] == "dark"
        assert b["interface.theme"] == "dark"

    async def test_set_preference_records_an_audit_event(self, spark_db: Any) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
        events: list[SettingChange] = []
        audit.set_audit_hook(events.append)
        await accessor.set_preference(spark_db, "user-a", "interface.theme", "dark")
        assert [(e.key, e.user_guid, e.action) for e in events] == [
            ("interface.theme", "user-a", "set")
        ]


class TestAudit:
    def test_a_failing_hook_never_breaks_the_change(self) -> None:
        def boom(change: SettingChange) -> None:
            raise RuntimeError("audit sink down")

        audit.set_audit_hook(boom)
        audit.record(SettingChange("voice.engine", "instance", "a", "set", False))

    def test_no_hook_is_a_no_op(self) -> None:
        audit.record(SettingChange("voice.engine", "instance", "a", "set", False))


class _Request:
    class _App:
        class _State:
            user_guid = "local-guid"

        state = _State()

    app = _App()


class TestAccessCheck:
    def test_default_stub_treats_the_signed_in_operator_as_administrator(self) -> None:
        check = access.access_check()
        assert check.is_administrator(_Request())  # type: ignore[arg-type]
        assert check.user_guid(_Request()) == "local-guid"  # type: ignore[arg-type]

    def test_replaceable(self) -> None:
        class Deny:
            def is_administrator(self, request: Any) -> bool:
                return False

            def user_guid(self, request: Any) -> str:
                return "someone"

        access.set_access_check(Deny())
        assert not access.access_check().is_administrator(_Request())  # type: ignore[arg-type]
        access.reset()
        assert access.access_check().is_administrator(_Request())  # type: ignore[arg-type]
