"""Model clients made from settings alone, for code that runs without the web app.

The worker builds knowledge graphs away from any web process, so it cannot
borrow the web app's :class:`~spark.llm.manager.LLMManager` or its
conversation manager. :func:`build_providers` makes the enabled providers from
any settings source with konfig's ``get`` (``config.yaml``, or a server's
instance settings view, which resolves secrets itself), by the same rules as
the web process; :class:`ModelClients` then hands out a client of its own for
one model.

Secrets are only ever passed to provider constructors; nothing here logs them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from spark.llm.base import LLMService
from spark.llm.manager import LLMManager

__all__ = ["ModelClients", "SecretResolver", "SettingsLike", "build_manager", "build_providers"]

logger = logging.getLogger(__name__)

SecretResolver = Callable[[str | None], str]


class SettingsLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


def _as_is(value: str | None) -> str:
    return str(value) if value else ""


def _anthropic(settings: SettingsLike, resolve: SecretResolver) -> LLMService | None:
    from spark.llm.anthropic_direct import AnthropicDirectProvider

    api_key = resolve(settings.get("providers.anthropic.api_key", ""))
    return AnthropicDirectProvider(api_key=api_key) if api_key else None


def _bedrock(settings: SettingsLike, resolve: SecretResolver) -> LLMService | None:
    from spark.llm.bedrock import BedrockProvider

    region = settings.get("providers.aws_bedrock.region", "us-east-1")
    auth_method = settings.get("providers.aws_bedrock.auth_method", "sso")
    read_timeout = int(settings.get("providers.aws_bedrock.read_timeout", 300) or 300)
    access_key = resolve(settings.get("providers.aws_bedrock.access_key"))
    secret_key = resolve(settings.get("providers.aws_bedrock.secret_key"))
    # Explicit keys only when the authentication method is not SSO.
    if auth_method in ("iam", "session") and access_key and secret_key:
        session_token = resolve(settings.get("providers.aws_bedrock.session_token"))
        return BedrockProvider(
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token or None,
            read_timeout=read_timeout,
        )
    return BedrockProvider(
        region=region,
        profile=settings.get("providers.aws_bedrock.profile"),
        read_timeout=read_timeout,
    )


def _ollama(settings: SettingsLike, resolve: SecretResolver) -> LLMService | None:
    from spark.llm.ollama import OllamaProvider

    return OllamaProvider(
        base_url=settings.get("providers.ollama.base_url", "http://localhost:11434")
    )


def _gemini(settings: SettingsLike, resolve: SecretResolver) -> LLMService | None:
    from spark.llm.google_gemini import GoogleGeminiProvider

    api_key = resolve(settings.get("providers.google_gemini.api_key", ""))
    return GoogleGeminiProvider(api_key=api_key) if api_key else None


def _xai(settings: SettingsLike, resolve: SecretResolver) -> LLMService | None:
    from spark.llm.xai import XAIProvider

    api_key = resolve(settings.get("providers.xai.api_key", ""))
    return XAIProvider(api_key=api_key) if api_key else None


# (settings prefix, display name for logs, builder), in the web process's order.
_PROVIDERS: tuple[
    tuple[str, str, Callable[[SettingsLike, SecretResolver], LLMService | None]], ...
] = (
    ("providers.anthropic", "Anthropic", _anthropic),
    ("providers.aws_bedrock", "AWS Bedrock", _bedrock),
    ("providers.ollama", "Ollama", _ollama),
    ("providers.google_gemini", "Google Gemini", _gemini),
    ("providers.xai", "X.AI", _xai),
)


def build_providers(settings: SettingsLike, resolve: SecretResolver = _as_is) -> list[LLMService]:
    """The enabled providers ``settings`` describes; one that fails to start is left out.

    ``resolve`` turns a stored secret reference into its value (as-is by
    default, for a source such as the instance settings view that already
    resolves secrets).
    """
    providers: list[LLMService] = []
    for prefix, label, make in _PROVIDERS:
        if not settings.get(f"{prefix}.enabled"):
            continue
        try:
            provider = make(settings, resolve)
        except Exception as exc:  # noqa: BLE001 - one provider must not stop the others
            logger.warning("Failed to init %s provider (%s)", label, type(exc).__name__)
            continue
        if provider is not None:
            providers.append(provider)
    return providers


def build_manager(settings: SettingsLike, resolve: SecretResolver = _as_is) -> LLMManager:
    """An :class:`LLMManager` holding the providers ``settings`` enables."""
    manager = LLMManager()
    for provider in build_providers(settings, resolve):
        manager.register_provider(provider)
    return manager


class ModelClients:
    """A client of its own per call for a model, from providers made once from settings.

    Each client is a copy of the provider that lists the model, with the model
    set on the copy (:meth:`LLMManager.service_for`), so concurrent builds on
    different models never change each other's.
    """

    def __init__(self, settings: SettingsLike, resolve: SecretResolver = _as_is) -> None:
        self._manager = build_manager(settings, resolve)

    def __call__(self, model_id: str) -> LLMService | None:
        return self._manager.service_for(model_id)
