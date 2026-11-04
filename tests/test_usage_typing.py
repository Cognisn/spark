"""mypy --strict over the usage modules new in 0.3.0 (A17).

CI's blocking mypy step lists its modules in the workflow, which this
workstream may not edit, so this test makes the check blocking until the
integration session adds these paths there (Skuld decision 47).
"""

from __future__ import annotations

from pathlib import Path

from mypy import api

ROOT = Path(__file__).resolve().parent.parent
MODULES = [
    "src/spark/usage",
    "src/spark/database/usage.py",
    "src/spark/web/endpoints/usage.py",
]


def test_usage_modules_pass_mypy_strict() -> None:
    paths = [str(ROOT / m) for m in MODULES]
    stdout, stderr, status = api.run(["--strict", "--no-error-summary", *paths])
    assert status == 0, stdout + stderr
