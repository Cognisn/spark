"""The overlay_connection_scope revision: upgrade and downgrade on SQLite and PostgreSQL.

The upgrade copies each overlay entry to every connection that offered it before
(its catalogue provider; a chat entry only where the connection is uncurated or has
the model enabled), keeps an entry no connection offers with no connection, and the
downgrade restores one entry per model, refusing while two connections declare one
model differently.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "overlay_connection_scope"
PREVIOUS = "credit_metering"
NOW = "2026-11-19T00:00:00+00:00"

Exec = Callable[[str], None]
Query = Callable[[str], list[tuple[Any, ...]]]


def _module() -> Any:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    return script.module


def _connection(cid: str, settings: dict[str, Any]) -> str:
    body = json.dumps(settings).replace("'", "''")
    return (
        "INSERT INTO connections (id, name, kind, settings, residency, created_at, updated_at) "
        f"VALUES ('{cid}', 'Example {cid}', '{settings['kind']}', '{body}', '{{}}', "
        f"'{NOW}', '{NOW}')"
    )


def _model(cid: str, model: str, enabled: bool) -> str:
    return (
        "INSERT INTO connection_models (connection_id, model_id, enabled, tier, updated_at) "
        f"VALUES ('{cid}', '{model}', {1 if enabled else 0}, 'standard', '{NOW}')"
    )


def _overlay(provider: str, model: str, *, kind: str = "chat", context: int = 8_192) -> str:
    limits = "1024, NULL" if kind == "chat" else "NULL, 768"
    return (
        "INSERT INTO catalogue_overlays (provider, model_id, kind, context_window, "
        "output_limit, embedding_dimensions, residency_scope, regions, updated_at) "
        f"VALUES ('{provider}', '{model}', '{kind}', {context}, {limits}, 'local', '[]', '{NOW}')"
    )


FOUNDRY_CLAUDE = {"kind": "anthropic", "api_key": "secret://k", "base_url": "https://x"}
DIRECT_CLAUDE = {"kind": "anthropic", "api_key": "secret://k"}
FOUNDRY_GROK = {
    "kind": "openai_compat",
    "service": {
        "name": "foundry",
        "endpoint": "https://example.openai.azure.com/openai/v1",
        "auth": {"mode": "api_key", "api_key": "secret://k"},
        "catalogue_provider": "xai",
    },
}
OPENAI = {"kind": "openai_compat", "service": {"name": "openai", "api_key": "secret://k"}}
OLLAMA = {"kind": "ollama", "base_url": "http://127.0.0.1:11434"}

#: Overlay rows after the upgrade, as (provider, model id, connection id).
EXPECTED = [
    ("anthropic", "sonnet-au-prod", "claude-direct"),  # uncurated: offered it
    ("anthropic", "sonnet-au-prod", "foundry-au"),  # curated, enabled
    ("gemini", "gemini-example", None),  # offered nowhere: kept with no connection
    ("ollama", "llama3.1:8b", "home-ollama"),  # uncurated
    ("ollama", "nomic-embed-text", "home-ollama"),  # embedding: every connection
    ("ollama", "nomic-embed-text", "lab-ollama"),
    ("ollama", "qwen3:32b", "home-ollama"),  # switched off on lab-ollama
    ("xai", "grok-au", "foundry-grok"),
]


def _seed(execute: Exec) -> None:
    execute(_connection("foundry-au", FOUNDRY_CLAUDE))
    execute(_connection("claude-direct", DIRECT_CLAUDE))
    execute(_connection("claude-curated", DIRECT_CLAUDE))
    execute(_connection("foundry-grok", FOUNDRY_GROK))
    execute(_connection("openai", OPENAI))
    execute(_connection("home-ollama", OLLAMA))
    execute(_connection("lab-ollama", OLLAMA))
    execute(_model("foundry-au", "sonnet-au-prod", True))
    execute(_model("claude-curated", "claude-sonnet-5", True))  # curated, without it
    execute(_model("lab-ollama", "llama3.1:8b", False))  # switched off there
    execute(_model("lab-ollama", "qwen3:32b", False))
    execute(_overlay("anthropic", "sonnet-au-prod"))
    execute(_overlay("ollama", "llama3.1:8b"))
    execute(_overlay("ollama", "nomic-embed-text", kind="embedding"))
    execute(_overlay("ollama", "qwen3:32b"))
    execute(_overlay("xai", "grok-au"))
    execute(_overlay("gemini", "gemini-example"))


def _scoped(query: Query) -> list[tuple[Any, ...]]:
    rows = query(
        "SELECT provider, model_id, connection_id FROM catalogue_overlays "
        "ORDER BY provider, model_id, COALESCE(connection_id, '')"
    )
    return [tuple(r) for r in rows]


def test_revision_follows_credit_metering() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_on_the_chain_to_head() -> None:
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


@pytest.mark.parametrize(
    "settings",
    [FOUNDRY_CLAUDE, DIRECT_CLAUDE, FOUNDRY_GROK, OPENAI, OLLAMA],
)
def test_frozen_catalogue_provider_matches_conduit(settings: dict[str, Any]) -> None:
    from conduit import LiteralSecretResolver, ProviderRegistry
    from conduit.providers import builtin_factories

    registry = ProviderRegistry(secrets=LiteralSecretResolver({}), factories=builtin_factories())
    validated = registry.validate("x", settings)
    assert _module().catalogue_provider(settings["kind"], settings) == (
        validated.catalogue_provider
    )


class _Scenario:
    """The shared upgrade and downgrade checks, for one engine."""

    settings: DatabaseSettings

    def execute(self, sql: str) -> None:
        raise NotImplementedError

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def integrity_errors(self) -> tuple[type[BaseException], ...]:
        raise NotImplementedError

    def test_upgrade_assigns_entries_to_the_connections_that_offered_them(self) -> None:
        migrate.upgrade(self.settings, target=PREVIOUS)
        _seed(self.execute)
        migrate.upgrade(self.settings)
        assert _scoped(self.query) == EXPECTED
        kept = self.query(
            "SELECT kind, context_window, output_limit, embedding_dimensions, residency_scope "
            "FROM catalogue_overlays WHERE model_id = 'nomic-embed-text' AND "
            "connection_id = 'lab-ollama'"
        )
        assert [tuple(r) for r in kept] == [("embedding", 8_192, None, 768, "local")]

    def test_upgrade_of_an_empty_table(self) -> None:
        migrate.upgrade(self.settings)
        assert _scoped(self.query) == []

    def test_one_entry_per_connection_and_model(self) -> None:
        migrate.upgrade(self.settings)
        self.execute(_connection("home-ollama", OLLAMA))
        scoped = (
            "INSERT INTO catalogue_overlays (provider, model_id, context_window, output_limit, "
            "residency_scope, updated_at, connection_id) VALUES ('ollama', 'm', 1, 1, 'local', "
            f"'{NOW}', {{cid}})"
        )
        self.execute(scoped.format(cid="'home-ollama'"))
        with pytest.raises(self.integrity_errors()):
            self.execute(scoped.format(cid="'home-ollama'"))
        self.execute(scoped.format(cid="NULL"))
        with pytest.raises(self.integrity_errors()):
            self.execute(scoped.format(cid="NULL"))

    def test_downgrade_restores_one_entry_per_model(self) -> None:
        migrate.upgrade(self.settings, target=PREVIOUS)
        _seed(self.execute)
        migrate.upgrade(self.settings)
        migrate.downgrade(self.settings, target=PREVIOUS)
        rows = self.query(
            "SELECT provider, model_id FROM catalogue_overlays ORDER BY provider, model_id"
        )
        assert [tuple(r) for r in rows] == [
            ("anthropic", "sonnet-au-prod"),
            ("gemini", "gemini-example"),
            ("ollama", "llama3.1:8b"),
            ("ollama", "nomic-embed-text"),
            ("ollama", "qwen3:32b"),
            ("xai", "grok-au"),
        ]
        # Back to the earlier key: one row per provider and model.
        with pytest.raises(self.integrity_errors()):
            self.execute(_overlay("xai", "grok-au"))
        migrate.upgrade(self.settings)
        assert _scoped(self.query) == EXPECTED

    def test_downgrade_refuses_differing_declarations(self) -> None:
        migrate.upgrade(self.settings)
        self.execute(_connection("home-ollama", OLLAMA))
        self.execute(_connection("lab-ollama", OLLAMA))
        for cid, context in (("home-ollama", 8_192), ("lab-ollama", 4_096)):
            self.execute(
                "INSERT INTO catalogue_overlays (provider, model_id, context_window, "
                "output_limit, residency_scope, updated_at, connection_id) VALUES "
                f"('ollama', 'm', {context}, 1, 'local', '{NOW}', '{cid}')"
            )
        with pytest.raises(RuntimeError, match="declared differently"):
            migrate.downgrade(self.settings, target=PREVIOUS)
        assert migrate.current(self.settings) == migrate.head()
        assert len(_scoped(self.query)) == 2


class TestSqlite(_Scenario):
    @pytest.fixture(autouse=True)
    def _settings(self, tmp_path: Path) -> None:
        self.settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")

    def _connect(self) -> sqlite3.Connection:
        assert self.settings.sqlite_path is not None
        return sqlite3.connect(self.settings.sqlite_path, isolation_level=None)

    def execute(self, sql: str) -> None:
        conn = self._connect()
        try:
            conn.execute(sql)
        finally:
            conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        conn = self._connect()
        try:
            return list(conn.execute(sql).fetchall())
        finally:
            conn.close()

    def integrity_errors(self) -> tuple[type[BaseException], ...]:
        return (sqlite3.IntegrityError,)


@pytest.mark.postgres
class TestPostgres(_Scenario):
    @pytest.fixture(autouse=True)
    def _settings(self, pg_url: str) -> None:
        self.settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        self.url = pg_url

    def execute(self, sql: str) -> None:
        import psycopg

        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("SET search_path TO spark")
            conn.execute(sql)  # type: ignore[arg-type]

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        import psycopg

        with psycopg.connect(self.url) as conn:
            conn.execute("SET search_path TO spark")
            return list(conn.execute(sql).fetchall())  # type: ignore[arg-type]

    def integrity_errors(self) -> tuple[type[BaseException], ...]:
        import psycopg

        return (psycopg.errors.UniqueViolation,)

    def test_deleting_a_connection_deletes_its_entries(self) -> None:
        migrate.upgrade(self.settings)
        self.execute(_connection("home-ollama", OLLAMA))
        self.execute(
            "INSERT INTO catalogue_overlays (provider, model_id, context_window, output_limit, "
            f"residency_scope, updated_at, connection_id) VALUES ('ollama', 'm', 1, 1, 'local', "
            f"'{NOW}', 'home-ollama')"
        )
        self.execute("DELETE FROM connections WHERE id = 'home-ollama'")
        assert _scoped(self.query) == []
