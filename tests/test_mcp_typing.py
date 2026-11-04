"""The MCP integration and its settings endpoint pass ``mypy --strict`` (A17).

CI's blocking strict list lives in the workflow; until these modules are on it,
this test keeps them strict through the test job.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES = [
    "src/spark/mcp_integration",
    "src/spark/web/endpoints/mcp_servers.py",
    "src/spark/instance_settings/consumers.py",
]


@pytest.mark.timeout(600)
def test_mcp_modules_are_strictly_typed() -> None:
    pytest.importorskip("mypy")
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", *MODULES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
