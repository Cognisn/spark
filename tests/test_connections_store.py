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
from spark.connections.catalogue import build_catalogue
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
            "ollama", "mistral-small:24b", 32_768, "local", output_limit=8_192
        )
        await repo.save_overlay(spark_db, overlay)
        snapshot = await load_snapshot(spark_db, store, server=True)
        assert [c.id for c in snapshot.connections] == ["home-ollama", "work-claude"]
        assert set(snapshot.models) == {"home-ollama"}  # only curated connections
        assert snapshot.assignments["work-claude"][0].role == "staff"
        assert snapshot.overlays == (overlay,)
        personal = await load_snapshot(spark_db, store, server=False)
        assert personal.assignments == {}


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
