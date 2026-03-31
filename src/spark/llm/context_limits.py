"""Context window and max output limit resolution."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default context windows and max outputs for known model families
_DEFAULTS: dict[str, tuple[int, int]] = {
    # Anthropic
    "claude-opus-4": (200_000, 32_000),
    "claude-sonnet-4": (200_000, 32_000),
    "claude-3-7-sonnet": (200_000, 32_000),
    "claude-3-5-sonnet": (200_000, 8_192),
    "claude-3-5-haiku": (200_000, 8_192),
    "claude-3-opus": (200_000, 4_096),
    "claude-3-sonnet": (200_000, 4_096),
    "claude-3-haiku": (200_000, 4_096),
    # Ollama
    "llama3": (128_000, 4_096),
    "llama3.1": (128_000, 4_096),
    "llama3.2": (128_000, 4_096),
    "llama3.3": (128_000, 4_096),
    "mistral": (32_768, 8_192),
    "mixtral": (32_768, 8_192),
    "qwen2": (128_000, 4_096),
    "codellama": (16_384, 4_096),
    "gemma": (8_192, 4_096),
    # Google Gemini
    "gemini-2.5": (1_000_000, 65_536),
    "gemini-2.0": (1_000_000, 8_192),
    "gemini-1.5-pro": (2_000_000, 8_192),
    "gemini-1.5-flash": (1_000_000, 8_192),
    # xAI
    "grok-4.1": (2_000_000, 131_072),
    "grok-4": (256_000, 16_384),
    "grok-3": (131_072, 8_192),
    "grok-3-mini": (131_072, 8_192),
}

_GLOBAL_DEFAULT = (8_192, 4_096)


class ContextLimitResolver:
    """Resolves context window and max output for a given model ID.

    Resolution priority:
      1. Exact match in config overrides
      2. Partial match (model_id contains pattern)
      3. Hardcoded defaults for known families
      4. Global default (8192 / 4096)
    """

    def __init__(self, config_overrides: dict[str, Any] | None = None) -> None:
        self._overrides: dict[str, tuple[int, int]] = {}
        if config_overrides:
            for key, val in config_overrides.items():
                if isinstance(val, dict):
                    ctx = val.get("context_window", _GLOBAL_DEFAULT[0])
                    out = val.get("max_output", _GLOBAL_DEFAULT[1])
                    self._overrides[key.lower()] = (ctx, out)

    def resolve(self, model_id: str) -> tuple[int, int]:
        """Return (context_window, max_output) for a model."""
        lower = model_id.lower()

        # 1. Exact match in overrides
        if lower in self._overrides:
            return self._overrides[lower]

        # 2. Partial match in overrides
        for pattern, limits in self._overrides.items():
            if pattern in lower:
                return limits

        # 3. Partial match in hardcoded defaults
        for pattern, limits in _DEFAULTS.items():
            if pattern in lower:
                return limits

        # 4. Global default
        logger.debug("No context limits found for %s, using defaults", model_id)
        return _GLOBAL_DEFAULT

    def get_context_window(self, model_id: str) -> int:
        """Get the context window size for a model."""
        return self.resolve(model_id)[0]

    def get_max_output(self, model_id: str) -> int:
        """Get the max output tokens for a model."""
        return self.resolve(model_id)[1]

    def get_compaction_threshold(self, model_id: str, ratio: float = 0.7) -> int:
        """Token count at which compaction should trigger."""
        return int(self.get_context_window(model_id) * ratio)

    def get_emergency_threshold(self, model_id: str, ratio: float = 0.95) -> int:
        """Token count at which emergency compaction fires."""
        return int(self.get_context_window(model_id) * ratio)
