"""mypy --strict over the projects packages (the wave gate's and CI's strict list does not
name them yet; the integration session adds them, then this test can go)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATHS = (
    "src/spark/projects",
    "src/spark/database/projects.py",
    "src/spark/database/project_members.py",
    "src/spark/web/endpoints/projects.py",
)


def test_the_projects_packages_type_check_strictly() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--platform", "linux", *PATHS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout + done.stderr
