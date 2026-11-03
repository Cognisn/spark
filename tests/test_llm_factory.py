"""The model client factory (``spark.llm.factory``): providers from settings, no web app.

Provider classes are replaced by stubs, so nothing reaches a model. The factory
follows the same rules as the web process's provider set-up: a provider is
built only when enabled, an API key provider only with a key, a ``secret://``
reference is resolved, and a provider that fails to start is left out.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from spark.llm import factory


class StubProvider:
    """A provider that lists two models and records how it was made."""

    name = "Stub"
    models = ("model-a", "model-b")

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.model: str | None = None

    def get_provider_name(self) -> str:
        return self.name

    def list_available_models(self) -> list[dict[str, Any]]:
        return [{"id": m, "name": m} for m in self.models]

    def set_model(self, model_id: str) -> None:
        self.model = model_id


def _stub(name: str, models: tuple[str, ...] = ("model-a", "model-b")) -> type[StubProvider]:
    return type(name, (StubProvider,), {"name": name, "models": models})


class Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[StubProvider]]:
    import spark.llm.anthropic_direct as anthropic
    import spark.llm.bedrock as bedrock
    import spark.llm.google_gemini as gemini
    import spark.llm.ollama as ollama
    import spark.llm.xai as xai

    made = {
        "anthropic": _stub("Anthropic", ("claude-x",)),
        "bedrock": _stub("AWS Bedrock", ("bedrock-x",)),
        "ollama": _stub("Ollama", ("llama-x",)),
        "gemini": _stub("Google Gemini", ("gemini-x",)),
        "xai": _stub("X.AI", ("grok-x",)),
    }
    monkeypatch.setattr(anthropic, "AnthropicDirectProvider", made["anthropic"])
    monkeypatch.setattr(bedrock, "BedrockProvider", made["bedrock"])
    monkeypatch.setattr(ollama, "OllamaProvider", made["ollama"])
    monkeypatch.setattr(gemini, "GoogleGeminiProvider", made["gemini"])
    monkeypatch.setattr(xai, "XAIProvider", made["xai"])
    return made


EVERY_PROVIDER = {
    "providers.anthropic.enabled": True,
    "providers.anthropic.api_key": "secret://providers.anthropic.api_key",
    "providers.aws_bedrock.enabled": True,
    "providers.aws_bedrock.region": "ap-southeast-2",
    "providers.ollama.enabled": True,
    "providers.ollama.base_url": "http://ollama.example.com:11434",
    "providers.google_gemini.enabled": True,
    "providers.google_gemini.api_key": "gemini-key-dev-only",
    "providers.xai.enabled": True,
    "providers.xai.api_key": "xai-key-dev-only",
}


def _resolve(value: str | None) -> str:
    return (
        "anthropic-key-dev-only" if value == "secret://providers.anthropic.api_key" else value or ""
    )


class TestBuildProviders:
    def test_every_enabled_provider_is_built(self, stubs: Any) -> None:
        providers = factory.build_providers(Settings(EVERY_PROVIDER), _resolve)
        names = [p.get_provider_name() for p in providers]
        assert names == ["Anthropic", "AWS Bedrock", "Ollama", "Google Gemini", "X.AI"]
        by_name = {p.get_provider_name(): p for p in providers}
        assert by_name["Anthropic"].kwargs == {"api_key": "anthropic-key-dev-only"}
        assert by_name["Ollama"].kwargs == {"base_url": "http://ollama.example.com:11434"}
        assert by_name["AWS Bedrock"].kwargs == {
            "region": "ap-southeast-2",
            "profile": None,
            "read_timeout": 300,
        }

    def test_disabled_and_keyless_providers_are_left_out(self, stubs: Any) -> None:
        settings = dict(EVERY_PROVIDER)
        settings["providers.xai.enabled"] = False
        settings["providers.google_gemini.api_key"] = ""
        settings["providers.aws_bedrock.enabled"] = False
        names = [p.get_provider_name() for p in factory.build_providers(Settings(settings))]
        assert names == ["Anthropic", "Ollama"]

    def test_bedrock_keys_for_iam(self, stubs: Any) -> None:
        settings = {
            "providers.aws_bedrock.enabled": True,
            "providers.aws_bedrock.auth_method": "iam",
            "providers.aws_bedrock.access_key": "AKIA-dev-only",
            "providers.aws_bedrock.secret_key": "secret-dev-only",
        }
        (provider,) = factory.build_providers(Settings(settings))
        assert provider.kwargs == {
            "region": "us-east-1",
            "access_key": "AKIA-dev-only",
            "secret_key": "secret-dev-only",
            "session_token": None,
            "read_timeout": 300,
        }

    def test_a_provider_that_fails_to_start_is_left_out(
        self, stubs: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.llm.ollama as ollama

        def broken(**_: Any) -> Any:
            raise RuntimeError("cannot reach the host")

        monkeypatch.setattr(ollama, "OllamaProvider", broken)
        names = [
            p.get_provider_name()
            for p in factory.build_providers(Settings(EVERY_PROVIDER), _resolve)
        ]
        assert "Ollama" not in names and "Anthropic" in names

    def test_matches_the_web_process(self, stubs: Any) -> None:
        """The web process builds the same providers from the same settings."""
        from spark.web import server

        class Ctx:
            secrets = None

        web = server._build_providers(Ctx(), Settings(EVERY_PROVIDER))  # type: ignore[arg-type]
        mine = factory.build_providers(Settings(EVERY_PROVIDER))
        assert [type(p) for p in mine] == [type(p) for p in web]
        assert [p.kwargs for p in mine] == [p.kwargs for p in web]


class TestModelClients:
    def test_a_client_of_its_own_for_a_listed_model(self, stubs: Any) -> None:
        clients = factory.ModelClients(Settings(EVERY_PROVIDER), _resolve)
        first = clients("gemini-x")
        second = clients("gemini-x")
        assert first is not None and second is not None
        assert first.get_provider_name() == "Google Gemini"
        assert first.model == "gemini-x"
        assert first is not second

    def test_no_client_for_a_model_no_provider_lists(self, stubs: Any) -> None:
        assert factory.ModelClients(Settings(EVERY_PROVIDER), _resolve)("gone-model") is None

    def test_no_providers_no_client(self, stubs: Any) -> None:
        assert factory.ModelClients(Settings({}))("claude-x") is None


def test_the_factory_needs_no_web_app() -> None:
    code = (
        "import sys, spark.llm.factory; "
        "loaded = sorted(m for m in sys.modules if m.startswith(('spark.web', 'fastapi'))); "
        "print(loaded); sys.exit(1 if loaded else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
