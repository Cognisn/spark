"""Tests for core application bootstrap."""

from pathlib import Path

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
        assert "port" not in defaults["interface"]  # Port is random on startup

    def test_logging_defaults(self) -> None:
        defaults = _default_settings()
        assert defaults["logging"]["level"] == "INFO"

    def test_conversation_defaults(self) -> None:
        defaults = _default_settings()
        assert defaults["conversation"]["rollup_threshold"] == 0.3
        assert defaults["conversation"]["max_tool_iterations"] == 25
