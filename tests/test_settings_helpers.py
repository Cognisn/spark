"""Tests for settings endpoint helper functions."""

from unittest.mock import MagicMock

from spark.web.endpoints.settings import _build_sections, _build_tool_categories, _set_nested


class TestSetNested:
    def test_single_level(self) -> None:
        d: dict = {}
        _set_nested(d, "key", "value")
        assert d == {"key": "value"}

    def test_two_levels(self) -> None:
        d: dict = {}
        _set_nested(d, "a.b", 42)
        assert d == {"a": {"b": 42}}

    def test_three_levels(self) -> None:
        d: dict = {}
        _set_nested(d, "a.b.c", True)
        assert d == {"a": {"b": {"c": True}}}

    def test_preserves_existing(self) -> None:
        d = {"a": {"x": 1}}
        _set_nested(d, "a.y", 2)
        assert d == {"a": {"x": 1, "y": 2}}

    def test_overwrites_existing(self) -> None:
        d = {"a": {"b": "old"}}
        _set_nested(d, "a.b", "new")
        assert d == {"a": {"b": "new"}}


class TestBuildSections:
    def test_returns_list(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        sections = _build_sections(settings)
        assert isinstance(sections, list)
        assert len(sections) > 0

    def test_all_sections_have_required_keys(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        sections = _build_sections(settings)
        for section in sections:
            assert "id" in section
            assert "title" in section
            assert "icon" in section
            assert "description" in section
            assert "groups" in section

    def test_section_ids(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        sections = _build_sections(settings)
        ids = {s["id"] for s in sections}
        assert "providers" in ids
        assert "database" in ids
        assert "interface" in ids
        assert "conversation" in ids
        assert "security" in ids
        assert "logging" in ids

    def test_provider_groups(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        sections = _build_sections(settings)
        providers = next(s for s in sections if s["id"] == "providers")
        group_ids = {g["id"] for g in providers["groups"]}
        assert "anthropic" in group_ids
        assert "aws_bedrock" in group_ids
        assert "ollama" in group_ids
        assert "google_gemini" in group_ids
        assert "xai" in group_ids

    def test_field_types(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        sections = _build_sections(settings)
        all_fields = []
        for section in sections:
            for group in section["groups"]:
                all_fields.extend(group["fields"])
        valid_types = {"toggle", "text", "number", "secret", "select", "model_select"}
        for field in all_fields:
            assert field["type"] in valid_types
            assert "key" in field
            assert "label" in field


class TestBuildToolCategories:
    def test_returns_list(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        cats = _build_tool_categories(settings)
        assert isinstance(cats, list)
        assert len(cats) == 4

    def test_category_ids(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        cats = _build_tool_categories(settings)
        ids = {c["id"] for c in cats}
        assert ids == {"filesystem", "documents", "archives", "web"}

    def test_category_structure(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        cats = _build_tool_categories(settings)
        for cat in cats:
            assert "id" in cat
            assert "title" in cat
            assert "icon" in cat
            assert "description" in cat
            assert "enabled" in cat
            assert "tools" in cat
            assert isinstance(cat["tools"], list)
            assert len(cat["tools"]) > 0

    def test_filesystem_has_mode_and_extras(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        cats = _build_tool_categories(settings)
        fs = next(c for c in cats if c["id"] == "filesystem")
        assert "read" in fs["mode_options"]
        assert "read_write" in fs["mode_options"]
        assert len(fs["extra_fields"]) >= 1
        assert fs["extra_fields"][0]["key"] == "embedded_tools.filesystem.allowed_paths"

    def test_web_has_no_mode(self) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=None)
        cats = _build_tool_categories(settings)
        web = next(c for c in cats if c["id"] == "web")
        assert web["mode"] is None
        assert web["mode_options"] == []

    def test_enabled_reads_from_settings(self) -> None:
        settings = MagicMock()

        def mock_get(key: str, default: object = None) -> object:
            if key == "embedded_tools.filesystem.enabled":
                return True
            if key == "embedded_tools.web.enabled":
                return False
            return default

        settings.get = mock_get
        cats = _build_tool_categories(settings)
        fs = next(c for c in cats if c["id"] == "filesystem")
        web = next(c for c in cats if c["id"] == "web")
        assert fs["enabled"] is True
        assert web["enabled"] is False
