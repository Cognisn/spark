"""LLM Manager — routes requests across multiple providers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)


class LLMManager:
    """Manages multiple LLM providers and routes requests to the active one."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMService] = {}
        self._active_provider: str | None = None
        self._offered: Callable[[], Sequence[str]] | None = None

    def replace_providers(self, providers: Iterable[LLMService]) -> None:
        """Swap in a new set of providers (a settings change); keep the active one if still present."""
        replaced = {p.get_provider_name(): p for p in providers}
        active = self._active_provider
        if active not in replaced:
            active = next(iter(replaced), None)
        self._providers, self._active_provider = replaced, active
        logger.info("LLM providers now: %s", ", ".join(replaced) or "none")

    def set_offered(self, offered: Callable[[], Sequence[str]] | None) -> None:
        """Limit the models listed and chosen to those ``offered()`` names (empty offers every model)."""
        self._offered = offered

    def offered_models(self) -> frozenset[str]:
        """The model ids users may choose from; empty when every model is offered."""
        return frozenset(self._offered()) if self._offered is not None else frozenset()

    def is_offered(self, model_id: str) -> bool:
        """Whether a user may choose ``model_id`` (the same rule as :meth:`list_all_models`).

        :meth:`set_model` never asks: a conversation pinned to a model that has
        left the list keeps working.
        """
        offered = self.offered_models()
        return not offered or model_id in offered

    def register_provider(self, provider: LLMService) -> None:
        """Register an LLM provider."""
        name = provider.get_provider_name()
        self._providers[name] = provider
        logger.info("Registered LLM provider: %s", name)
        if self._active_provider is None:
            self._active_provider = name

    @property
    def active_provider(self) -> str | None:
        """Name of the currently active provider."""
        return self._active_provider

    @property
    def active_service(self) -> LLMService | None:
        """The currently active LLMService instance."""
        if self._active_provider:
            return self._providers.get(self._active_provider)
        return None

    @property
    def providers(self) -> dict[str, LLMService]:
        """All registered providers."""
        return dict(self._providers)

    def set_model(self, model_id: str, *, provider_name: str | None = None) -> None:
        """Set the active model, optionally specifying the provider.

        If provider_name is not given, searches all providers for the model.
        """
        if provider_name:
            if provider_name not in self._providers:
                raise ValueError(f"Unknown provider: {provider_name}")
            self._providers[provider_name].set_model(model_id)
            self._active_provider = provider_name
            logger.info("Model set to %s on %s", model_id, provider_name)
            return

        # Search all providers
        for name, provider in self._providers.items():
            models = provider.list_available_models()
            if any(m["id"] == model_id for m in models):
                provider.set_model(model_id)
                self._active_provider = name
                logger.info("Model set to %s on %s", model_id, name)
                return

        raise ValueError(f"Model {model_id} not found in any provider")

    def list_all_models(self) -> list[dict[str, Any]]:
        """List all models across all registered providers (only those offered, when limited)."""
        models = []
        for provider in self._providers.values():
            models.extend(provider.list_available_models())
        offered = self.offered_models()
        if offered:
            models = [m for m in models if m.get("id") in offered]
        return models

    def invoke_model(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the active provider's model."""
        service = self.active_service
        if service is None:
            raise RuntimeError("No active LLM provider")
        return service.invoke_model(messages, **kwargs)

    def count_tokens(self, text: str) -> int:
        """Count tokens using the active provider, or estimate."""
        service = self.active_service
        if service:
            return service.count_tokens(text)
        return len(text) // 4

    def supports_streaming(self) -> bool:
        """Whether the active provider supports streaming."""
        service = self.active_service
        return service.supports_streaming() if service else False
