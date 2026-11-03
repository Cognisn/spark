"""What a personal-mode export reads besides the database, for the web export and ``spark export``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from spark import files as files_pkg
from spark.bundles.exporter import ExportSources, user_preferences
from spark.files.area import FileAreaNotConfigured
from spark.instance_settings import registry

__all__ = ["bundle_filename", "personal_sources", "secret_values"]


def secret_values(secrets: Any) -> list[str]:
    """The values the secrets store holds for SPARK's secret settings (to redact)."""
    values: list[str] = []
    for key in registry.secret_keys():
        for name in (registry.secret_name(key), registry.legacy_secret_name(key)):
            try:
                value = secrets.get(name)
            except Exception:  # noqa: BLE001 - an unreadable secret is simply not there
                continue
            if isinstance(value, str) and value:
                values.append(value)
    return values


def personal_sources(settings: Any, secrets: Any) -> ExportSources:
    """The file area, the user's skills, their preferences, the MCP servers, the secrets."""
    from spark.skills.manager import get_skills_manager

    try:
        store: Any = files_pkg.file_store()
    except FileAreaNotConfigured:
        store = None
    preferences = {
        key: value
        for key in user_preferences(dict.fromkeys(registry.keys(registry.Scope.USER)))
        if (value := settings.get(key, None)) is not None
    }
    servers = settings.get("mcp.servers", None)
    return ExportSources(
        files=store,
        skills_dir=get_skills_manager().user_dir,
        preferences=preferences,
        mcp_servers=servers if isinstance(servers, list) else [],
        secret_values=secret_values(secrets),
    )


def bundle_filename(bundle_id: str) -> str:
    return f"spark-bundle-{datetime.now(UTC):%Y%m%d}-{bundle_id[:8]}.zip"
