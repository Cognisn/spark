"""LLM Manager — routes requests across multiple providers."""

from __future__ import annotations

import logging
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)


class LLMManager:
    """Manages multiple LLM providers and routes requests to the active one."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMService] = {}
        self._active_provider: str | None = None

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
        """List all models across all registered providers."""
        models = []
        for provider in self._providers.values():
            models.extend(provider.list_available_models())
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
