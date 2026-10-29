"""The tray daemon and the background daemon refuse server mode; personal mode is unchanged."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from spark.daemon import guard
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

PG_URL = "postgresql://spark:spark-dev-only@db.example.com:5432/spark"
SERVER = RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url=PG_URL))


def _personal(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    )


class TestGuard:
    def test_server_mode_is_refused_with_a_clear_message(self) -> None:
        with pytest.raises(guard.ServerModeRefused) as exc:
            guard.refuse_server_mode(SERVER)
        message = str(exc.value)
        assert "personal mode" in message
        assert "spark worker" in message

    def test_personal_mode_passes(self, tmp_path: Path) -> None:
        guard.refuse_server_mode(_personal(tmp_path))


class _IconStarted(Exception):
    pass


def _fake_pystray() -> types.ModuleType:
    module = types.ModuleType("pystray")

    def _icon(*args: object, **kwargs: object) -> None:
        raise _IconStarted

    class _Menu:
        SEPARATOR = object()

        def __init__(self, *items: object) -> None:
            pass

    module.Icon = _icon  # type: ignore[attr-defined]
    module.Menu = _Menu  # type: ignore[attr-defined]
    module.MenuItem = lambda *a, **k: None  # type: ignore[attr-defined]
    return module


class TestTrayDaemon:
    def test_server_mode_stops_before_the_icon_or_pid_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from spark.daemon import tray

        monkeypatch.setitem(sys.modules, "pystray", _fake_pystray())
        monkeypatch.setattr(guard, "load_runtime", lambda: SERVER)
        daemon = tray.SparkTrayDaemon()
        monkeypatch.setattr(daemon, "_pid_file", lambda: tmp_path / "tray.pid")
        with pytest.raises(guard.ServerModeRefused):
            daemon.run()
        assert not (tmp_path / "tray.pid").exists()

    def test_personal_mode_still_starts_the_icon(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from spark.daemon import tray

        monkeypatch.setitem(sys.modules, "pystray", _fake_pystray())
        monkeypatch.setattr(guard, "load_runtime", lambda: _personal(tmp_path))
        daemon = tray.SparkTrayDaemon()
        monkeypatch.setattr(daemon, "_pid_file", lambda: tmp_path / "tray.pid")
        with pytest.raises(_IconStarted):
            daemon.run()

    def test_entry_point_exits_2_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from spark.daemon import tray

        monkeypatch.setitem(sys.modules, "pystray", _fake_pystray())
        monkeypatch.setattr(guard, "load_runtime", lambda: SERVER)
        with pytest.raises(SystemExit) as exc:
            tray.run_tray_daemon()
        assert exc.value.code == 2
        assert "spark worker" in capsys.readouterr().err


class TestBackgroundDaemon:
    def test_server_mode_never_starts_the_runner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from spark.daemon import app
        from spark.scheduler import runner

        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text(f"mode: server\ndatabase:\n  url: {PG_URL}\n")

        def _refuse(*args: object, **kwargs: object) -> None:
            pytest.fail("the runner must not start in server mode")

        monkeypatch.setattr(runner, "ActionRunner", _refuse)
        import spark.db.standalone as standalone

        monkeypatch.setattr(standalone, "open_standalone", _refuse)
        app._shutdown.clear()
        app.run_daemon()
        assert "spark worker" in capsys.readouterr().err


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in ("APPDATA", "LOCALAPPDATA"):
        env[name] = str(tmp_path / name)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        env[name] = str(tmp_path / name)
    env["HOME"] = str(tmp_path / "home")
    env["SPARK__MODE"] = "server"
    env["SPARK__DATABASE__URL"] = PG_URL
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    return env


@pytest.mark.timeout(120)
def test_the_tray_process_refuses_server_mode(tmp_path: Path) -> None:
    """End to end: ``python -m spark.daemon.tray`` in server mode exits 2 with the message."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "spark.daemon.tray"],
            env=_isolated_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:  # the child is killed; a running tray is a failure
        pytest.fail("the tray daemon started in server mode instead of refusing")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "spark worker" in result.stderr
