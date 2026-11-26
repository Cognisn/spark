"""SPARK in personal mode on this host, for the live suite's personal-mode checks.

The process runs from this repository with every per-user folder (``APPDATA``,
``LOCALAPPDATA``, ``HOME``, the XDG paths) inside the sandbox and secrets in an
encrypted file there with a dev-only key, so nothing touches this machine's own
SPARK data or keychain. Its one Ollama connection points at the stack's model stub
on its published port, and the client signs in with the start-up login code the
process logs, as the auto-login link would.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.live.client import Browser

REPO = Path(__file__).resolve().parents[2]
_CODE = re.compile(r"Authentication code: (\S+)")
_META = re.compile(r'<meta name="csrf-token" content="([^"]*)"')


def personal_env(home: Path, port: int | None = None) -> dict[str, str]:
    """The environment of a personal-mode SPARK whose every folder is under ``home``."""
    from cryptography.fernet import Fernet

    keep = {k: v for k, v in os.environ.items() if not k.startswith(("SPARK__", "KONFIG_"))}
    real_cache = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    if real_cache.is_dir():
        keep["HF_HOME"] = str(real_cache)
    key_file = home / "konfig.key"
    if not key_file.exists():
        home.mkdir(parents=True, exist_ok=True)
        key_file.write_text(Fernet.generate_key().decode(), encoding="utf-8", newline="\n")
    env = keep | {
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SPARK__MODE": "personal",
        "SPARK__SECRETS__BACKEND": "encrypted_file",
        "SPARK__SECRETS__FILE_PATH": str(home / "secrets.enc"),
        "KONFIG_MASTER_KEY": key_file.read_text("utf-8").strip(),
        "SPARK__INTERFACE__OPEN_BROWSER": "false",
        "PYTHONIOENCODING": "utf-8",
    }
    if port is not None:
        env["SPARK__INTERFACE__PORT"] = str(port)
    return env


def config_path(env: dict[str, str]) -> Path:
    done = subprocess.run(  # noqa: S603 - this repository's own code
        [
            sys.executable,
            "-c",
            "from spark.core.application import _get_config_path; print(_get_config_path())",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return Path(done.stdout.strip().splitlines()[-1])


class PersonalApp:
    """A running personal-mode SPARK and a signed-in client for it."""

    def __init__(self, home: Path, port: int, stub_port: int) -> None:
        self.home = home
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.env = personal_env(home, port)
        self.log = home / "spark.log"
        config = config_path(self.env)
        config.parent.mkdir(parents=True, exist_ok=True)
        if not config.exists():
            settings: dict[str, Any] = {
                "providers": {
                    "ollama": {"enabled": True, "base_url": f"http://127.0.0.1:{stub_port}"}
                },
                "interface": {"open_browser": False},
                "tool_permissions": {"auto_approve": True},
            }
            config.write_text(yaml.safe_dump(settings), encoding="utf-8", newline="\n")
        self.process: subprocess.Popen[bytes] | None = None
        self.browser: Browser | None = None

    def start(self) -> Browser:
        out = self.log.open("a", encoding="utf-8", newline="\n")
        self.process = subprocess.Popen(  # noqa: S603 - this repository's own entry point
            [sys.executable, "-c", "import sys; from spark.launch import main; sys.exit(main())"],
            cwd=REPO,
            env=self.env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 900
        while True:
            assert self.process.poll() is None, f"SPARK exited; see {self.log}"
            try:
                status = httpx.get(f"{self.url}/loading/api/status", timeout=5)
                if status.status_code == 200 and status.json().get("ready"):
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, f"personal SPARK not ready; see {self.log}"
            time.sleep(2)
        self.browser = self.sign_in()
        return self.browser

    def sign_in(self) -> Browser:
        """Redeem the newest login code the process logged, as the auto-login link does."""
        codes = _CODE.findall(self.log.read_text("utf-8", errors="replace"))
        assert codes, f"no login code in {self.log}"
        browser = Browser(self.url)
        landed = browser.navigate(f"/auto-login?code={codes[-1]}")
        assert landed.status_code == 200, (landed.status_code, landed.text[:500])
        page = browser.navigate("/conversations")
        token = _META.search(page.text)
        assert token, page.text[:2000]
        browser.csrf = token.group(1)
        return browser

    def stop(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=30)
        self.process = None


def export_bundle(home: Path, target: Path) -> subprocess.CompletedProcess[str]:
    """``spark export`` (the CLI's own export) of the personal user under ``home``."""
    return subprocess.run(  # noqa: S603 - this repository's own CLI
        [
            sys.executable,
            "-c",
            "import sys; from spark.bundles import cli; "
            "sys.exit(cli.run_export(['--output', sys.argv[1]]))",
            str(target),
        ],
        cwd=REPO,
        env=personal_env(home),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
