"""mypy --strict over the bundle modules, so every CI test job blocks on it (A17, D51).

The CI workflow's own strict list was read-only for this workstream; the
integration session can move these paths there and drop this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STRICT = ["src/spark/bundles", "src/spark/web/endpoints/bundles.py"]


@pytest.mark.timeout(600)
def test_bundle_modules_pass_mypy_strict() -> None:
    from mypy import api

    stdout, stderr, status = api.run(
        ["--strict", "--no-incremental", "--config-file", str(ROOT / "pyproject.toml")]
        + [str(ROOT / p) for p in STRICT]
    )
    assert status == 0, stdout + stderr
