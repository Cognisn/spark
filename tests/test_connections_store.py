"""Connection stores: config.yaml in personal mode, the database on a server, and the
snapshot the runtime applies (with SPARK's catalogue overlay)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from conduit import ResidencyPolicy, ResidencyScope, default_catalogue

from spark.connections import Connection, ConnectionInvalid, ConnectionModel
from spark.connections.catalogue import (
    Candidate,
    build_catalogue,
    catalogue_provider,
    connection_catalogue,
    offering_connections,
    own_entries,
)
from spark.connections.store import DatabaseConnectionStore, FileConnectionStore, load_snapshot
from spark.database import connections as repo

CLAUDE = Connection(
    "work-claude",
    "Work Claude",
    {"kind": "anthropic", "api_key": "secret://connections.work-claude.api_key"},
)
LOCAL = Connection(
    "home-ollama",
    "Home Ollama",
    {"kind": "ollama", "base_url": "http://192.0.2.40:11434"},
    residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL})),
)


class TestFileStore:
    def test_round_trip_keeps_other_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("interface:\n  port: 22600\n", encoding="utf-8")
        store = FileConnectionStore(path)
        asyncio.run(store.save_connection(CLAUDE))
        asyncio.run(store.save_connection(LOCAL))
        assert asyncio.run(store.list_connections()) == [LOCAL, CLAUDE]
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["interface"] == {"port": 22600}
        assert b"\r\n" not in path.read_bytes()

    def test_models_kept_per_connection(self, tmp_path: Path) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")
        asyncio.run(store.save_connection(LOCAL))
        asyncio.run(store.save_model(ConnectionModel("home-ollama", "llama3.1:8b", True, "lower")))
        asyncio.run(store.save_model(ConnectionModel("home-ollama", "qwen2.5:72b", False)))
        # Saving the connection again keeps its models.
        asyncio.run(store.save_connection(LOCAL))
        assert asyncio.run(store.list_models("home-ollama")) == [
            ConnectionModel("home-ollama", "llama3.1:8b", True, "lower"),
            ConnectionModel("home-ollama", "qwen2.5:72b", False, "standard"),
        ]
        with pytest.raises(ConnectionInvalid):
            asyncio.run(store.save_model(ConnectionModel("missing", "m")))

    def test_plain_text_credential_never_written(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        store = FileConnectionStore(path)
        bad = Connection("work", "Work", {"kind": "gemini", "api_key": "plain-example-value"})
        with pytest.raises(ConnectionInvalid, match="api_key"):
            asyncio.run(store.save_connection(bad))
        assert not path.exists() or "plain-example" not in path.read_text(encoding="utf-8")

    def test_delete(self, tmp_path: Path) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")
        asyncio.run(store.save_connection(CLAUDE))
        assert asyncio.run(store.delete_connection("work-claude")) is True
        assert asyncio.run(store.delete_connection("work-claude")) is False
        assert asyncio.run(store.list_connections()) == []

    def test_parallel_writers_lose_nothing(self, tmp_path: Path) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")

        async def write_all() -> None:
            await asyncio.gather(
                *(
                    store.save_connection(Connection(f"c{i}", f"C{i}", {"kind": "ollama"}))
                    for i in range(10)
                )
            )

        asyncio.run(write_all())
        assert sorted(c.id for c in asyncio.run(store.list_connections())) == sorted(
            f"c{i}" for i in range(10)
        )


@pytest.mark.asyncio
class TestDatabaseStore:
    async def test_round_trip(self, spark_db: Any) -> None:
        store = DatabaseConnectionStore(spark_db)
        await store.save_connection(CLAUDE)
        await store.save_connection(LOCAL)
        assert await store.list_connections() == [LOCAL, CLAUDE]
        assert await store.get_connection("work-claude") == CLAUDE
        await store.save_model(ConnectionModel("work-claude", "claude-sonnet-5", True, "higher"))
        assert await store.list_models() == [
            ConnectionModel("work-claude", "claude-sonnet-5", True, "higher")
        ]
        assert await store.delete_connection("work-claude") is True
        assert await store.list_models() == []

    async def test_snapshot(self, spark_db: Any) -> None:
        store = DatabaseConnectionStore(spark_db)
        await store.save_connection(CLAUDE)
        await store.save_connection(LOCAL)
        await store.save_model(ConnectionModel("home-ollama", "llama3.1:8b"))
        await repo.save_assignment(spark_db, repo.Assignment("work-claude", role="staff"))
        overlay = repo.OverlayRow(
            "ollama",
            "mistral-small:24b",
            32_768,
            "local",
            output_limit=8_192,
            connection_id="home-ollama",
        )
        await store.save_overlay(overlay)
        snapshot = await load_snapshot(spark_db, store, server=True)
        assert [c.id for c in snapshot.connections] == ["home-ollama", "work-claude"]
        assert set(snapshot.models) == {"home-ollama"}  # only curated connections
        assert snapshot.assignments["work-claude"][0].role == "staff"
        assert snapshot.overlays == (overlay,)
        personal = await load_snapshot(spark_db, store, server=False)
        assert personal.assignments == {}

    async def test_overlays_per_connection(self, spark_db: Any) -> None:
        store = DatabaseConnectionStore(spark_db)
        await store.save_connection(LOCAL)
        await store.save_connection(LAB)
        await store.save_overlay(_entry("home-ollama", context=8_192))
        await store.save_overlay(_entry("lab-ollama", context=4_096))
        assert await store.list_overlays("home-ollama") == [_entry("home-ollama", context=8_192)]
        assert [o.connection_id for o in await store.list_overlays()] == [
            "home-ollama",
            "lab-ollama",
        ]


LAB = Connection(
    "lab-ollama",
    "Lab Ollama",
    {"kind": "ollama", "base_url": "http://192.0.2.41:11434"},
    residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL})),
)


def _entry(
    cid: str | None, model: str = "qwen3:32b", *, context: int = 32_768, kind: str = "chat"
) -> repo.OverlayRow:
    return repo.OverlayRow(
        "ollama",
        model,
        context,
        "local",
        kind="embedding" if kind == "embedding" else "chat",
        output_limit=8_192 if kind == "chat" else None,
        embedding_dimensions=768 if kind == "embedding" else None,
        tools=True,
        connection_id=cid,
    )


class TestFileStoreOverlays:
    def test_kept_in_each_connections_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        store = FileConnectionStore(path)
        asyncio.run(store.save_connection(LOCAL))
        asyncio.run(store.save_connection(LAB))
        asyncio.run(store.save_overlay(_entry("home-ollama")))
        asyncio.run(store.save_overlay(_entry("lab-ollama", "nomic", kind="embedding")))
        assert asyncio.run(store.list_overlays("home-ollama")) == [_entry("home-ollama")]
        assert asyncio.run(store.list_overlays()) == [
            _entry("lab-ollama", "nomic", kind="embedding"),
            _entry("home-ollama"),
        ]
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        home = next(e for e in raw["connections"] if e["id"] == "home-ollama")
        assert [o["model_id"] for o in home["overlays"]] == ["qwen3:32b"]
        assert "connection_id" not in home["overlays"][0]
        assert b"\r\n" not in path.read_bytes()

    def test_declared_again_replaces_that_connections_entry_only(self, tmp_path: Path) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")
        asyncio.run(store.save_connection(LOCAL))
        asyncio.run(store.save_connection(LAB))
        asyncio.run(store.save_overlay(_entry("home-ollama")))
        asyncio.run(store.save_overlay(_entry("lab-ollama")))
        asyncio.run(store.save_overlay(_entry("home-ollama", context=65_536)))
        assert asyncio.run(store.list_overlays()) == [
            _entry("home-ollama", context=65_536),
            _entry("lab-ollama"),
        ]

    def test_saving_or_deleting_the_connection(self, tmp_path: Path) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")
        asyncio.run(store.save_connection(LOCAL))
        asyncio.run(store.save_overlay(_entry("home-ollama")))
        asyncio.run(store.save_connection(LOCAL))  # an edit keeps its entries
        assert asyncio.run(store.list_overlays()) == [_entry("home-ollama")]
        asyncio.run(store.delete_connection("home-ollama"))
        assert asyncio.run(store.list_overlays()) == []

    def test_refused_without_a_stored_connection(self, tmp_path: Path) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")
        asyncio.run(store.save_connection(LOCAL))
        with pytest.raises(ConnectionInvalid, match="belongs to a connection"):
            asyncio.run(store.save_overlay(_entry(None)))
        with pytest.raises(ConnectionInvalid, match="no connection"):
            asyncio.run(store.save_overlay(_entry("missing")))
        bad = repo.OverlayRow(
            "ollama", "m", 1, "everywhere", output_limit=1, connection_id="home-ollama"
        )
        with pytest.raises(ConnectionInvalid, match="residency scope"):
            asyncio.run(store.save_overlay(bad))
        assert asyncio.run(store.list_overlays()) == []

    def test_an_unreadable_entry_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        store = FileConnectionStore(path)
        asyncio.run(store.save_connection(LOCAL))
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["connections"][0]["overlays"] = [{"provider": "ollama"}, "junk"]
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert asyncio.run(store.list_overlays()) == []


@pytest.mark.asyncio
class TestPersonalUnscopedEntries:
    """Entries stored in the database before they were scoped move into config.yaml."""

    async def _legacy(self, spark_db: Any, entry: repo.OverlayRow) -> None:
        await spark_db.execute(
            "INSERT INTO catalogue_overlays (provider, model_id, kind, context_window, "
            "output_limit, embedding_dimensions, tools, residency_scope, regions, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 'local', '[]', '2026-11-19T00:00:00+00:00')",
            (
                entry.provider,
                entry.model_id,
                entry.kind,
                entry.context_window,
                entry.output_limit,
                entry.embedding_dimensions,
            ),
        )

    async def test_moved_to_the_connections_that_offered_them(
        self, spark_db: Any, tmp_path: Path
    ) -> None:
        store = FileConnectionStore(tmp_path / "config.yaml")
        await store.save_connection(LOCAL)  # uncurated: offered everything
        await store.save_connection(LAB)
        await store.save_model(ConnectionModel("lab-ollama", "qwen3:32b", enabled=False))
        await store.save_connection(CLAUDE)
        await self._legacy(spark_db, _entry(None))
        await self._legacy(spark_db, _entry(None, "nomic", kind="embedding"))
        await self._legacy(spark_db, repo.OverlayRow("gemini", "g", 1_000, "local", output_limit=1))

        snapshot = await load_snapshot(spark_db, store, server=False)

        assert [(o.model_id, o.connection_id) for o in snapshot.overlays] == [
            ("nomic", "home-ollama"),
            ("nomic", "lab-ollama"),
            ("qwen3:32b", "home-ollama"),
        ]
        assert await repo.list_overlays(spark_db) == []  # gemini's: offered nowhere, gone
        # A second load finds nothing more to move.
        again = await load_snapshot(spark_db, store, server=False)
        assert again.overlays == snapshot.overlays

    async def test_a_server_keeps_them_unapplied(self, spark_db: Any) -> None:
        store = DatabaseConnectionStore(spark_db)
        await store.save_connection(LOCAL)
        await self._legacy(spark_db, _entry(None))
        snapshot = await load_snapshot(spark_db, store, server=True)
        assert snapshot.overlays == (_entry(None),)


class TestCatalogue:
    def test_overlay_adds_and_overrides(self) -> None:
        base = default_catalogue()
        shipped = base.require("ollama", "llama3.1:8b")
        rows = [
            repo.OverlayRow("ollama", "mistral-small:24b", 32_768, "local", output_limit=8_192),
            repo.OverlayRow("ollama", "llama3.1:8b", 16_384, "local", output_limit=2_048),
            repo.OverlayRow("ollama", "bad-entry", 1_000, "local"),  # a chat model needs a limit
        ]
        catalogue = build_catalogue(rows, base)
        assert catalogue.require("ollama", "mistral-small:24b").context_window == 32_768
        assert catalogue.require("ollama", "llama3.1:8b").context_window == 16_384
        assert shipped.context_window != 16_384
        assert catalogue.get("ollama", "bad-entry") is None
        assert catalogue.overlays[-1] == "spark"

    def test_no_rows_is_the_default(self) -> None:
        assert build_catalogue([]) is default_catalogue()

    def test_a_connection_catalogue_takes_its_own_entries_only(self) -> None:
        rows = [_entry("home-ollama"), _entry("lab-ollama", "nomic"), _entry(None, "orphan")]
        assert own_entries(rows, "home-ollama") == [_entry("home-ollama")]
        catalogue = connection_catalogue(rows, "lab-ollama", default_catalogue())
        assert catalogue.get("ollama", "nomic") is not None
        assert catalogue.get("ollama", "qwen3:32b") is None
        assert catalogue.get("ollama", "orphan") is None

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            ({"kind": "anthropic", "api_key": "secret://k"}, "anthropic"),
            ({"kind": "ollama"}, "ollama"),
            ({"kind": "openai_compat", "service": {"name": "xai"}}, "xai"),
            ({"kind": "openai_compat", "service": {"name": "foundry"}}, "openai"),
            (
                {
                    "kind": "openai_compat",
                    "service": {"name": "foundry", "catalogue_provider": "xai"},
                },
                "xai",
            ),
            ({"kind": "openai_compat"}, "openai_compat"),
        ],
    )
    def test_catalogue_provider(self, settings: dict[str, Any], expected: str) -> None:
        assert catalogue_provider(settings) == expected

    def test_offering_connections(self) -> None:
        candidates = [
            Candidate("home", "ollama"),  # uncurated
            Candidate("lab", "ollama", {"qwen3:32b": False, "other": True}),
            Candidate("box", "ollama", {"qwen3:32b": True}),
            Candidate("claude", "anthropic"),
        ]
        assert offering_connections(_entry(None), candidates) == ["box", "home"]
        embedding = _entry(None, "nomic", kind="embedding")
        assert offering_connections(embedding, candidates) == ["box", "home", "lab"]
        assert offering_connections(_entry(None), []) == []
