"""Packaging metadata: uv-friendly console entry point."""

from __future__ import annotations

import tomllib
from pathlib import Path


def _scripts() -> dict:
    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    return data["project"]["scripts"]


def test_cognisn_spark_alias_exists() -> None:
    # `uvx cognisn-spark` runs the command matching the package name, so an
    # alias makes the natural invocation work without `--from`.
    scripts = _scripts()
    assert scripts.get("cognisn-spark") == "spark.launch:main"


def test_primary_spark_command_preserved() -> None:
    assert _scripts().get("spark") == "spark.launch:main"


def test_konfig_declared_with_its_aws_extra() -> None:
    """Server mode's AWS Secrets Manager backends need konfig and its ``aws`` extra (Q6); 0.5.0 reloads its encrypted store across replicas (wave 5 D67), and 0.5.1 makes the AWS bundle backend read its own writes, so a write is never based on a stale read (rc22 deployment)."""
    root = Path(__file__).resolve().parent.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    assert [d for d in deps if d.startswith("cognisn-konfig")] == ["cognisn-konfig[aws]>=0.5.1"]


def _project() -> dict:
    root = Path(__file__).resolve().parent.parent
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]


def test_warden_declared_from_pypi() -> None:
    """Server-mode identity is cognisn-warden from PyPI (A10), at its 0.1.1 release or later
    (A18), which takes the client address on sign-in method and single sign-on settings
    changes: never a pre-release pin."""
    deps = _project()["dependencies"]
    assert [d for d in deps if d.startswith("cognisn-warden")] == ["cognisn-warden>=0.1.1"]


def test_conduit_declared_from_pypi_with_every_provider() -> None:
    """Every model is reached through cognisn-conduit from PyPI, with the extra of every
    service a connection may use (M4, M13) and its MCP client (M2), and no provider SDK
    declared directly."""
    deps = _project()["dependencies"]
    [conduit] = [d for d in deps if d.startswith("cognisn-conduit")]
    extras = set(conduit[conduit.index("[") + 1 : conduit.index("]")].split(","))
    assert extras == {
        "anthropic",
        "bedrock",
        "bedrock-mantle",
        "gemini",
        "konfig",
        "mcp",
        "ollama",
        "openai",
    }
    assert conduit.endswith(">=0.1.0")
    sdks = ("anthropic", "boto3", "botocore", "google-genai", "ollama", "openai")
    assert [d for d in deps if d.split(">")[0].split("=")[0].split("[")[0] in sdks] == []


def test_no_direct_references_anywhere() -> None:
    """No Git, path or URL requirement: every dependency installs from the index (A10)."""
    root = Path(__file__).resolve().parent.parent
    project = _project()
    requirements = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    assert [r for r in requirements if "@" in r or "git+" in r or "file:" in r] == []
    for name in ("Dockerfile", ".github/workflows/ci.yml", ".github/workflows/container.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "git+https://github.com/Cognisn/warden" not in text
        assert "warden.git" not in text
