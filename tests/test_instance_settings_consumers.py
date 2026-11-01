"""Settings consumers in server mode: providers, models offered and embedded tools.

The provider set-up, the models offered and the conversation manager's embedded
tools configuration read the instance settings (and the secrets store) in server
mode, and follow every change without a restart.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark import mode
from spark.instance_settings import consumers, registry
from spark.llm.manager import LLMManager
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings


class Source:
    """A settings source with registry defaults and overrides, and change observers."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})
        self.observers: list[Any] = []
        self.generation = 0

    def get(self, key: str) -> Any:
        return self.values.get(key, registry.spec(key).default)

    def on_change(self, observer: Any) -> None:
        self.observers.append(observer)

    def change(self, **values: Any) -> None:
        self.values.update({k.replace("__", "."): v for k, v in values.items()})
        self.generation += 1
        for observer in self.observers:
            observer()


class Secrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def resolve(self, key: str) -> str:
        return self.values.get(key, "")


class FakeProvider:
    def __init__(self, name: str, models: list[str]) -> None:
        self.name = name
        self.models = models

    def get_provider_name(self) -> str:
        return self.name

    def list_available_models(self) -> list[dict[str, Any]]:
        return [{"id": m, "name": m} for m in self.models]

    def set_model(self, model_id: str) -> None:
        self.model = model_id


@pytest.fixture(autouse=True)
def _server_mode(tmp_path: Path) -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class TestInstanceView:
    def test_plain_settings_come_from_the_source(self) -> None:
        view = consumers.InstanceView(Source({"providers.ollama.enabled": True}), Secrets())
        assert view.get("providers.ollama.enabled") is True
        assert view.get("providers.ollama.base_url") == "http://localhost:11434"

    def test_secrets_come_from_the_secrets_store(self) -> None:
        view = consumers.InstanceView(
            Source(), Secrets({"providers.anthropic.api_key": "sk-view-dev-only"})
        )
        assert view.get("providers.anthropic.api_key") == "sk-view-dev-only"

    def test_unknown_key_gives_the_default_and_cast_applies(self) -> None:
        view = consumers.InstanceView(Source({"providers.aws_bedrock.read_timeout": 90}), Secrets())
        assert view.get("not.a.setting", "fallback") == "fallback"
        assert view.get("providers.aws_bedrock.read_timeout", cast=str) == "90"

    def test_an_unset_secret_gives_the_default(self) -> None:
        view = consumers.InstanceView(Source(), Secrets())
        assert view.get("providers.xai.api_key", "") == ""


class TestEmbeddedToolsConfig:
    def test_tree_built_from_instance_settings_with_secrets_resolved(self) -> None:
        source = Source(
            {"embedded_tools.web.enabled": False, "embedded_tools.web.search_engine": "brave"}
        )
        config = consumers.embedded_tools_config(
            source, Secrets({"embedded_tools.web.brave_api_key": "brave-dev-only"})
        )
        web = config["embedded_tools"]["web"]
        assert web["enabled"] is False
        assert web["search_engine"] == "brave"
        assert web["brave_api_key"] == "brave-dev-only"
        assert config["embedded_tools"]["tool_results"]["web"] == "warn"

    def test_local_only_categories_are_left_out_on_a_server(self) -> None:
        tree = consumers.embedded_tools_config(Source(), Secrets())["embedded_tools"]
        assert {"filesystem", "system_commands", "email"}.isdisjoint(tree)
        assert {"documents", "archives", "web", "agents", "tool_results"} <= set(tree)

    def test_prompt_inspection_injected_only_when_enabled(self) -> None:
        assert "_prompt_inspection_enabled" not in consumers.embedded_tools_config(
            Source(), Secrets()
        )
        config = consumers.embedded_tools_config(
            Source({"prompt_inspection.enabled": True, "prompt_inspection.level": "strict"}),
            Secrets(),
        )
        assert config["_prompt_inspection_enabled"] is True
        assert config["_prompt_inspection_level"] == "strict"
        assert config["_prompt_inspection_action"] == "warn"


class TestModelsOffered:
    def _manager(self) -> LLMManager:
        mgr = LLMManager()
        mgr.register_provider(FakeProvider("one", ["model-a", "model-b"]))  # type: ignore[arg-type]
        return mgr

    def test_empty_offers_every_model(self) -> None:
        mgr = self._manager()
        mgr.set_offered(lambda: [])
        assert [m["id"] for m in mgr.list_all_models()] == ["model-a", "model-b"]

    def test_only_offered_models_are_listed(self) -> None:
        mgr = self._manager()
        mgr.set_offered(lambda: ["model-b"])
        assert [m["id"] for m in mgr.list_all_models()] == ["model-b"]

    def test_no_filter_by_default(self) -> None:
        assert len(self._manager().list_all_models()) == 2


class TestReplaceProviders:
    def test_providers_swapped_and_active_kept_when_still_present(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(FakeProvider("one", ["a"]))  # type: ignore[arg-type]
        mgr.register_provider(FakeProvider("two", ["b"]))  # type: ignore[arg-type]
        mgr.set_model("b")
        mgr.replace_providers([FakeProvider("two", ["b"]), FakeProvider("three", ["c"])])  # type: ignore[list-item]
        assert sorted(mgr.providers) == ["three", "two"]
        assert mgr.active_provider == "two"

    def test_active_falls_back_when_its_provider_goes(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(FakeProvider("one", ["a"]))  # type: ignore[arg-type]
        mgr.replace_providers([FakeProvider("three", ["c"])])  # type: ignore[list-item]
        assert mgr.active_provider == "three"
        mgr.replace_providers([])
        assert mgr.active_provider is None


class TestConversationManagerUpdates:
    def _manager(self) -> Any:
        from spark.core.conversation_manager import ConversationManager
        from spark.llm.context_limits import ContextLimitResolver

        return ConversationManager(
            None,  # type: ignore[arg-type]
            LLMManager(),
            ContextLimitResolver(None),
            embedded_tools_config={
                "embedded_tools": {"web": {"enabled": True}},
                "_memory_index": "kept",
                "_prompt_inspection_enabled": True,
            },
        )

    def test_new_config_keeps_run_time_keys_and_drops_tool_cache(self) -> None:
        mgr = self._manager()
        mgr._builtin_tools = [{"name": "stale"}]
        mgr.update_embedded_tools_config({"embedded_tools": {"web": {"enabled": False}}})
        assert mgr._embedded_tools_config["embedded_tools"]["web"]["enabled"] is False
        assert mgr._embedded_tools_config["_memory_index"] == "kept"
        assert "_prompt_inspection_enabled" not in mgr._embedded_tools_config
        assert mgr._builtin_tools is None and mgr._builtin_tool_names is None

    def test_tool_limits(self) -> None:
        mgr = self._manager()
        mgr.update_tool_limits(max_tool_iterations=3, max_tool_result_tokens=100)
        assert (mgr._max_tool_iterations, mgr._max_tool_selections) == (3, 30)
        assert mgr._max_tool_result_tokens == 100


class Conversations:
    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []
        self.limits: list[dict[str, Any]] = []

    def update_embedded_tools_config(self, config: dict[str, Any]) -> None:
        self.configs.append(config)

    def update_tool_limits(self, **limits: Any) -> None:
        self.limits.append(limits)


class TestFollower:
    def _follow(
        self, source: Source, builds: list[Any]
    ) -> tuple[LLMManager, Conversations, consumers.SettingsFollower]:
        mgr = LLMManager()
        conversations = Conversations()

        def build(view: Any) -> list[Any]:
            builds.append(view.get("providers.anthropic.enabled"))
            if view.get("providers.anthropic.enabled"):
                return [FakeProvider("anthropic", ["model-a", "model-b"])]
            return []

        follower = consumers.follow(
            source,  # type: ignore[arg-type]
            Secrets(),
            llm_manager=mgr,
            conversation_manager=conversations,
            build_providers=build,
        )
        return mgr, conversations, follower

    def test_a_provider_change_rebuilds_the_providers(self) -> None:
        source, builds = Source(), []
        mgr, _, follower = self._follow(source, builds)
        source.change(providers__anthropic__enabled=True)
        assert _wait_for(lambda: "anthropic" in mgr.providers)
        follower.wait_idle()

    def test_an_unrelated_change_does_not_rebuild_the_providers(self) -> None:
        source, builds = Source({"providers.anthropic.enabled": True}), []
        mgr, conversations, follower = self._follow(source, builds)
        mgr.replace_providers([FakeProvider("anthropic", ["model-a"])])  # type: ignore[list-item]
        source.change(embedded_tools__web__enabled=False)
        assert _wait_for(lambda: len(conversations.configs) == 1)
        follower.wait_idle()
        assert builds == []

    def test_models_offered_follow_the_setting(self) -> None:
        source, builds = Source({"providers.anthropic.enabled": True}), []
        mgr, _, follower = self._follow(source, builds)
        mgr.replace_providers([FakeProvider("anthropic", ["model-a", "model-b"])])  # type: ignore[list-item]
        source.change(models__offered=["model-a"])
        assert [m["id"] for m in mgr.list_all_models()] == ["model-a"]
        follower.wait_idle()

    def test_embedded_tools_and_limits_follow_every_change(self) -> None:
        source, builds = Source(), []
        _, conversations, follower = self._follow(source, builds)
        source.change(embedded_tools__web__enabled=False, conversation__max_tool_iterations=9)
        assert _wait_for(lambda: len(conversations.configs) == 1)
        follower.wait_idle()
        assert conversations.configs[-1]["embedded_tools"]["web"]["enabled"] is False
        assert conversations.limits[-1]["max_tool_iterations"] == 9

    def test_observer_returns_at_once(self) -> None:
        """The listener calls observers on the event loop: the work runs on a thread."""
        source, builds = Source(), []
        block = threading.Event()
        _, conversations, follower = self._follow(source, builds)
        conversations.update_embedded_tools_config = lambda c: block.wait(5)  # type: ignore[method-assign]
        started = time.monotonic()
        source.change(embedded_tools__web__enabled=False)
        assert time.monotonic() - started < 0.5
        block.set()
        follower.wait_idle()

    def test_a_failing_refresh_is_logged_and_the_next_change_still_applies(self) -> None:
        source, builds = Source(), []
        _, conversations, follower = self._follow(source, builds)
        calls: list[int] = []

        def flaky(config: dict[str, Any]) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            conversations.configs.append(config)

        conversations.update_embedded_tools_config = flaky  # type: ignore[method-assign]
        source.change(embedded_tools__web__enabled=False)
        assert _wait_for(lambda: len(calls) == 1)
        follower.wait_idle()
        source.change(embedded_tools__web__enabled=True)
        assert _wait_for(lambda: len(conversations.configs) == 1)
        follower.wait_idle()
