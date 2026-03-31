"""Abstract base class for LLM provider services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMService(ABC):
    """Contract that every LLM provider must implement."""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Human-readable provider name."""

    @abstractmethod
    def get_access_info(self) -> str:
        """Description of how the provider is authenticated."""

    @abstractmethod
    def list_available_models(self) -> list[dict[str, Any]]:
        """Return models available through this provider.

        Each model dict should contain at minimum:
          - id: str (model identifier)
          - name: str (human-friendly name)
          - provider: str (provider name)
          - supports_tools: bool
          - context_length: int
        """

    @abstractmethod
    def set_model(self, model_id: str) -> None:
        """Set the active model for subsequent requests."""

    @abstractmethod
    def invoke_model(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        stream_callback: Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the model and return a normalised response.

        The response dict must contain:
          - content: str (text response)
          - stop_reason: str ('end_turn', 'tool_use', 'max_tokens')
          - usage: dict (input_tokens, output_tokens)
          - tool_use: list | None (tool call blocks if stop_reason == 'tool_use')
          - content_blocks: list (array of content blocks)
        """

    @abstractmethod
    def supports_streaming(self) -> bool:
        """Whether this provider supports token-by-token streaming."""

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Estimate token count for a string."""

    def get_rate_limits(self) -> dict[str, Any] | None:
        """Optional: return rate limit info for this provider."""
        return None

    @property
    def current_model(self) -> str | None:
        """Currently selected model ID (override if needed)."""
        return getattr(self, "_model_id", None)
