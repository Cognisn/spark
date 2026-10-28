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
