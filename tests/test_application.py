"""Tests for core application bootstrap."""

from pathlib import Path

import pytest

from spark.core.application import (
    _default_settings,
    _ensure_config,
    _get_config_path,
    _get_data_path,
    _get_default_db_path,
)


class TestPlatformPaths:
    def test_config_path_not_cwd(self) -> None:
        config_path = _get_config_path()
        assert config_path != Path("config.yaml")
        assert "spark" in str(config_path)
        assert config_path.name == "config.yaml"

    def test_data_path_not_cwd(self) -> None:
        data_path = _get_data_path()
        assert "spark" in str(data_path)

    def test_db_path_inside_data_dir(self) -> None:
        db_path = _get_default_db_path()
        data_path = str(_get_data_path())
        assert db_path.startswith(data_path)
        assert db_path.endswith("spark.db")


class TestEnsureConfig:
    def test_creates_config_from_template(self, tmp_path: Path) -> None:
        config_path = tmp_path / "subdir" / "config.yaml"
        resources = Path(__file__).resolve().parent.parent / "src" / "spark" / "resources"
        assert (resources / "config.yaml.template").exists()

        first_run = _ensure_config(config_path)
        assert first_run is True
        assert config_path.exists()
        content = config_path.read_text()
        assert "database" in content

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.yaml"
        _ensure_config(config_path)
        assert config_path.exists()

    def test_existing_config_not_overwritten(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("existing: true\n")
        first_run = _ensure_config(config_path)
        assert first_run is False
        assert config_path.read_text() == "existing: true\n"


class TestDefaultSettings:
    def test_returns_dict(self) -> None:
        defaults = _default_settings()
        assert isinstance(defaults, dict)

    def test_has_required_sections(self) -> None:
        defaults = _default_settings()
        assert "logging" in defaults
        assert "database" in defaults
        assert "interface" in defaults
        assert "providers" in defaults
        assert "conversation" in defaults
        assert "tool_permissions" in defaults
        assert "embedded_tools" in defaults

    def test_database_defaults_use_platform_path(self) -> None:
        defaults = _default_settings()
        assert defaults["database"]["type"] == "sqlite"
        db_path = defaults["database"]["path"]
        # Should be an absolute path inside the platform data directory, not "spark.db"
        assert "spark.db" in db_path
        assert Path(db_path).is_absolute()

    def test_interface_defaults(self) -> None:
        defaults = _default_settings()
        assert defaults["interface"]["host"] == "127.0.0.1"
        # port 0 keeps the random-port-on-startup default while allowing a fixed
        # port to be set for headless/reverse-proxy runs.
        assert defaults["interface"]["port"] == 0
        assert defaults["interface"]["open_browser"] is True

    def test_logging_defaults(self) -> None:
        defaults = _default_settings()
        assert defaults["logging"]["level"] == "INFO"

    def test_conversation_defaults(self) -> None:
        defaults = _default_settings()
        assert defaults["conversation"]["rollup_threshold"] == pytest.approx(0.3)
        assert defaults["conversation"]["max_tool_iterations"] == 25


class _Settings:
    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class TestRuntimeConfiguration:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from spark import mode

        yield
        mode.reset()

    def test_mode_defaults_to_personal(self) -> None:
        assert _default_settings()["mode"] == "personal"

    def test_personal_defaults_configure_sqlite_in_data_dir(self) -> None:
        from spark import mode
        from spark.core.application import configure_runtime

        rs = configure_runtime(_Settings(_default_settings()))
        assert mode.current() is rs
        assert rs.mode is mode.Mode.PERSONAL
        assert rs.database.sqlite_path == Path(_get_default_db_path())

    def test_server_mode_with_sqlite_refuses_to_start(self) -> None:
        from spark import mode
        from spark.core.application import configure_runtime

        data = _default_settings()
        data["mode"] = "server"
        data["database"]["url"] = "sqlite:///spark.db"
        with pytest.raises(mode.ModeConfigError):
            configure_runtime(_Settings(data))
        with pytest.raises(RuntimeError):
            mode.current()

    def test_server_mode_with_postgresql(self) -> None:
        from spark import mode
        from spark.core.application import configure_runtime

        data = _default_settings()
        data["mode"] = "server"
        data["database"]["url"] = "postgresql://spark@db.example.com/spark"
        assert configure_runtime(_Settings(data)).mode is mode.Mode.SERVER
        assert mode.is_server()
