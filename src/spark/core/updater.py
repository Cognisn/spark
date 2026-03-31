"""Auto-update checker and updater for Spark.

Checks GitHub releases for newer versions and provides update functionality.
Supports both PyApp binary installs (self-update) and pip installs.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

GITHUB_OWNER = "Cognisn"
GITHUB_REPO = "spark"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


@dataclass
class UpdateInfo:
    """Information about an available update."""

    available: bool = False
    current_version: str = ""
    latest_version: str = ""
    release_url: str = ""
    release_notes: str = ""
    is_prerelease: bool = False
    install_method: str = "unknown"  # "pyapp" or "pip"


def get_current_version() -> str:
    """Get the current installed version of Spark."""
    import spark

    return spark.__version__


def is_pyapp_install() -> bool:
    """Check if Spark is running under a PyApp binary."""
    # PyApp sets PYAPP=1 and PYAPP_COMMAND_NAME in the environment
    return os.environ.get("PYAPP") == "1" or bool(shutil.which("pyapp"))


def get_install_method() -> str:
    """Determine how Spark was installed."""
    if is_pyapp_install():
        return "pyapp"
    return "pip"


def _parse_version(version_str: str) -> tuple:
    """Parse a version string into comparable tuple.

    Handles formats like: 0.1.0, 0.1.0a1, 0.1.0b2, 0.1.0rc1, v0.1.0
    """
    v = version_str.lstrip("v").strip()

    # Split pre-release suffix
    for sep in ("a", "b", "rc"):
        if sep in v:
            parts = v.split(sep, 1)
            base = tuple(int(x) for x in parts[0].split("."))
            pre_num = int(parts[1]) if parts[1].isdigit() else 0
            # a < b < rc < release: a=-3, b=-2, rc=-1, release=0
            pre_weight = {"a": -3, "b": -2, "rc": -1}[sep]
            return (*base, pre_weight, pre_num)

    # No pre-release suffix — full release
    return (*tuple(int(x) for x in v.split(".")), 0, 0)


def _is_newer(latest: str, current: str) -> bool:
    """Check if latest version is newer than current."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except (ValueError, TypeError):
        logger.warning("Could not compare versions: %s vs %s", latest, current)
        return False


def check_for_update(include_prereleases: bool = False) -> UpdateInfo:
    """Check GitHub for a newer release.

    Args:
        include_prereleases: If True, also check pre-release versions.
    """
    import json
    import urllib.request

    current = get_current_version()
    info = UpdateInfo(current_version=current, install_method=get_install_method())

    try:
        if include_prereleases:
            # Fetch all releases and find the latest (including pre-releases)
            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=5"
        else:
            url = GITHUB_API_URL

        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        if include_prereleases and isinstance(data, list):
            if not data:
                return info
            release = data[0]  # Most recent release
        else:
            release = data

        tag = release.get("tag_name", "")
        latest = tag.lstrip("v")

        if _is_newer(latest, current):
            info.available = True
            info.latest_version = latest
            info.release_url = release.get("html_url", "")
            info.release_notes = release.get("body", "")[:500]
            info.is_prerelease = release.get("prerelease", False)

        logger.info(
            "Update check: current=%s, latest=%s, available=%s",
            current,
            latest,
            info.available,
        )

    except Exception as e:
        logger.debug("Update check failed (non-fatal): %s", e)

    return info


def apply_update() -> dict[str, Any]:
    """Apply an available update.

    Returns a dict with status and message.
    """
    method = get_install_method()

    if method == "pyapp":
        return _update_pyapp()
    else:
        return _update_pip()


def _update_pyapp() -> dict[str, Any]:
    """Update via PyApp's self-update mechanism."""
    try:
        # PyApp binaries support: <binary> self update
        exe = sys.executable
        # Find the PyApp binary — it's the parent process or the binary itself
        pyapp_bin = os.environ.get("PYAPP_COMMAND_NAME", exe)

        result = subprocess.run(
            [pyapp_bin, "self", "update"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0:
            return {
                "status": "success",
                "message": "Update downloaded. Please restart Spark to apply.",
                "needs_restart": True,
            }
        else:
            return {
                "status": "error",
                "message": f"PyApp update failed: {result.stderr.strip()}",
                "needs_restart": False,
            }

    except Exception as e:
        logger.error("PyApp update error: %s", e)
        return {"status": "error", "message": str(e), "needs_restart": False}


def _update_pip() -> dict[str, Any]:
    """Update via pip upgrade."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "cognisn-spark"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0:
            return {
                "status": "success",
                "message": "Update installed. Please restart Spark to apply.",
                "needs_restart": True,
            }
        else:
            return {
                "status": "error",
                "message": f"pip upgrade failed: {result.stderr.strip()[:200]}",
                "needs_restart": False,
            }

    except Exception as e:
        logger.error("pip update error: %s", e)
        return {"status": "error", "message": str(e), "needs_restart": False}
