"""Server mode: the code that uses settings follows the administrator's instance settings (D34).

* :class:`InstanceView` gives code written against konfig (``get(key, default, cast=)``)
  the instance settings, with secrets from the secrets store.
* :func:`embedded_tools_config` builds the conversation manager's embedded tools
  configuration from the instance settings.
* :func:`follow` keeps the providers, the models offered, the embedded tools
  configuration and the tool limits current: every reload or change of the
  :class:`~spark.instance_settings.accessor.ServerSettings` (the listener's, or this
  replica's own) refreshes them on a worker thread, without a restart.

Personal mode does not use this module: it keeps reading ``config.yaml``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol

from spark.instance_settings import registry
from spark.instance_settings.accessor import SettingsSource
from spark.instance_settings.registry import Scope

__all__ = [
    "ConversationTarget",
    "InstanceView",
    "ObservableSource",
    "ProviderTarget",
    "SecretResolver",
    "SettingsFollower",
    "embedded_tools_config",
    "follow",
]

logger = logging.getLogger(__name__)

_EMBEDDED = "embedded_tools."
_PROVIDERS = "providers."
_LIMITS = {
    "max_tool_iterations": "conversation.max_tool_iterations",
    "max_tool_selections": "conversation.max_tool_selections",
    "max_tool_result_tokens": "conversation.max_tool_result_tokens",
}


class SecretResolver(Protocol):
    def resolve(self, key: str) -> str: ...


class ObservableSource(SettingsSource, Protocol):
    def on_change(self, observer: Callable[[], None]) -> None: ...


class ProviderTarget(Protocol):
    def replace_providers(self, providers: Iterable[Any]) -> None: ...

    def set_offered(self, offered: Callable[[], Sequence[str]] | None) -> None: ...


class ConversationTarget(Protocol):
    def update_embedded_tools_config(self, config: dict[str, Any]) -> None: ...

    def update_tool_limits(self, **limits: int) -> None: ...


class InstanceView:
    """konfig's ``get`` over the instance settings, with secrets from the secrets store.

    Server side only: secrets are resolved for provider clients and never reach a page.
    """

    def __init__(self, source: SettingsSource, secrets: SecretResolver) -> None:
        self._source = source
        self._secrets = secrets

    def get(
        self, key: str, default: Any = None, *, cast: Callable[[Any], Any] | None = None
    ) -> Any:
        if not registry.is_known(key):
            return default
        value = self._secrets.resolve(key) if registry.spec(key).secret else self._source.get(key)
        if value is None or (registry.spec(key).secret and value == ""):
            return default
        return cast(value) if cast is not None else value


def _available(category: str) -> bool:
    from spark.tools.registry import is_category_available

    return category == "tool_results" or is_category_available(category)


def embedded_tools_config(source: SettingsSource, secrets: SecretResolver) -> dict[str, Any]:
    """The conversation manager's embedded tools configuration from the instance settings.

    Categories this mode does not offer (the local-only tools on a server) are left
    out; prompt inspection is passed in the ``_prompt_inspection_*`` keys as at start-up.
    """
    tree: dict[str, Any] = {}
    for key in registry.keys(Scope.INSTANCE):
        if not key.startswith(_EMBEDDED):
            continue
        category, *rest = key[len(_EMBEDDED) :].split(".")
        if not rest or not _available(category):
            continue
        value = secrets.resolve(key) if registry.spec(key).secret else source.get(key)
        node = tree.setdefault(category, {})
        for part in rest[:-1]:
            node = node.setdefault(part, {})
        node[rest[-1]] = value
    config: dict[str, Any] = {"embedded_tools": tree}
    if source.get("prompt_inspection.enabled"):
        config["_prompt_inspection_enabled"] = True
        config["_prompt_inspection_level"] = source.get("prompt_inspection.level")
        config["_prompt_inspection_action"] = source.get("prompt_inspection.action")
    return config


def _provider_snapshot(view: InstanceView) -> tuple[Any, ...]:
    return tuple(view.get(k) for k in registry.keys(Scope.INSTANCE) if k.startswith(_PROVIDERS))


class SettingsFollower:
    """Refreshes the settings consumers after each change, one refresh at a time, on a thread.

    Changes that arrive during a refresh are coalesced into one more refresh.
    """

    def __init__(
        self,
        source: SettingsSource,
        secrets: SecretResolver,
        *,
        llm_manager: ProviderTarget,
        conversation_manager: ConversationTarget | None,
        build_providers: Callable[[InstanceView], Iterable[Any]],
    ) -> None:
        self._source = source
        self._secrets = secrets
        self._view = InstanceView(source, secrets)
        self._llm = llm_manager
        self._conversations = conversation_manager
        self._build_providers = build_providers
        self._providers_seen = _provider_snapshot(self._view)
        self._lock = threading.Lock()
        self._pending = False
        self._running = False
        self._idle = threading.Event()
        self._idle.set()

    def changed(self) -> None:
        """Observer for :meth:`ServerSettings.on_change`: returns at once."""
        with self._lock:
            self._pending = True
            self._idle.clear()
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="spark-settings-follower", daemon=True).start()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait until no refresh is pending or running (tests and shutdown)."""
        return self._idle.wait(timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._running = False
                    self._idle.set()
                    return
                self._pending = False
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the next change must still apply
                logger.exception("Applying a settings change failed")

    def refresh(self) -> None:
        """Bring the providers (when their settings changed), tools and limits up to date."""
        snapshot = _provider_snapshot(self._view)
        if snapshot != self._providers_seen:
            self._llm.replace_providers(self._build_providers(self._view))
            self._providers_seen = snapshot
            logger.info("LLM providers rebuilt after a settings change")
        if self._conversations is not None:
            self._conversations.update_embedded_tools_config(
                embedded_tools_config(self._source, self._secrets)
            )
            self._conversations.update_tool_limits(
                **{arg: int(self._source.get(key)) for arg, key in _LIMITS.items()}
            )


def follow(
    source: ObservableSource,
    secrets: SecretResolver,
    *,
    llm_manager: ProviderTarget,
    conversation_manager: ConversationTarget | None,
    build_providers: Callable[[InstanceView], Iterable[Any]],
) -> SettingsFollower:
    """Have the consumers follow ``source`` from now on; the models offered are read live."""

    def offered() -> Sequence[str]:
        value = source.get("models.offered")
        return [str(v) for v in value] if isinstance(value, list) else []

    llm_manager.set_offered(offered)
    follower = SettingsFollower(
        source,
        secrets,
        llm_manager=llm_manager,
        conversation_manager=conversation_manager,
        build_providers=build_providers,
    )
    source.on_change(follower.changed)
    return follower
