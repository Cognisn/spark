"""Exporting a personal-mode user's bundle (A15): what it holds, and what it never holds."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from spark.bundles import format as bf
from spark.bundles import secrets
from spark.bundles.exporter import ExportSources, export_bundle
from spark.bundles.reader import Limits, open_bundle
from spark.files.store import MountedVolumeStore
from tests.bundle_seed import OTHER_USER, Seeded, seed, write_skill

USER = "personal-user-guid"
CREDENTIAL = "sk-" + "proj-" + "Z9" * 12
SECRET_VALUE = "brave-" + "search-" + "value-0042"


@pytest.fixture
async def db(sqlite_settings: Any) -> AsyncIterator[Any]:
    from spark.db import Database

    database = await Database.open(sqlite_settings)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def store(tmp_path: Path) -> MountedVolumeStore:
    return MountedVolumeStore(tmp_path / "files")


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    path = tmp_path / "skills"
    write_skill(path)
    return path


@pytest.fixture
async def seeded(db: Any, store: MountedVolumeStore) -> Seeded:
    return await seed(db, USER, store)


def _sources(store: Any, skills_dir: Path | None, **overrides: Any) -> ExportSources:
    values: dict[str, Any] = {
        "files": store,
        "skills_dir": skills_dir,
        "preferences": {"interface.theme": "light", "default_model.model_id": "model-alpha"},
        "mcp_servers": [
            {"name": "local-notes", "transport": "stdio", "command": "notes-server"},
            {"name": "remote-search", "transport": "http", "url": "https://mcp.example.com/x"},
        ],
        "secret_values": [SECRET_VALUE],
    }
    values.update(overrides)
    return ExportSources(**values)


async def _export(db: Any, sources: ExportSources) -> tuple[bytes, Any]:
    out = io.BytesIO()
    report = await export_bundle(db, USER, out, sources, spark_version="0.3.0a10")
    return out.getvalue(), report


def _text_of(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return "\n".join(zf.read(n).decode("utf-8", "replace") for n in zf.namelist())


async def test_the_bundle_holds_every_section_and_opens(
    db: Any, store: Any, skills_dir: Path, seeded: Seeded
) -> None:
    data, report = await _export(db, _sources(store, skills_dir))
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        counts = bundle.manifest["counts"]
        assert counts == report.counts
        expected = {
            "conversations": 2,
            "messages": 3,
            "rollup_history": 1,
            "conversation_files": 1,
            "conversation_links": 1,
            "conversation_model_usage": 1,
            "conversation_tool_permissions": 2,
            "conversation_embedded_tools": 3,
            "conversation_mcp_servers": 3,
            "conversation_skills": 1,
            "context_index_elements": 1,
            "mcp_transactions": 1,
            "agent_runs": 1,
            "debate_config": 1,
            "debate_agents": 1,
            "debate_turns": 1,
            "debate_exhibits": 1,
            "user_memories": 2,
            "kg_nodes": 3,
            "kg_edges": 1,
            "kg_builds": 2,
            "skills": 1,
            "global_tool_permissions": 3,
            "autonomous_actions": 1,
            "action_tool_permissions": 3,
            "action_runs": 1,
            "user_files": 2,
        }
        assert counts == expected
        assert set(counts) == set(bf.TABLES)
        assert [r["id"] for r in bundle.rows("conversations")] == seeded.conversations
        for row in bundle.rows("user_files"):
            assert bundle.blob(row["blob"]) == seeded.files[row["storage_key"]]
        assert [(s, p) for s, p, _ in bundle.skill_files()] == [
            ("note-taker", "SKILL.md"),
            ("note-taker", "templates/meeting.txt"),
        ]
        assert bundle.preferences() == {
            "interface.theme": "light",
            "default_model.model_id": "model-alpha",
        }
        # Only each referenced server's transport travels, never its configuration.
        assert bundle.manifest["mcp_transports"] == {
            "local-notes": "stdio",
            "remote-search": "http",
            "retired-server": "unknown",
        }
        assert bundle.manifest["source_mode"] == "personal"
    assert (
        report.bundle_id
        == json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))["bundle_id"]
    )


async def test_no_owner_and_no_other_users_data(db: Any, store: Any, seeded: Seeded) -> None:
    data, _ = await _export(db, _sources(store, None))
    text = _text_of(data)
    assert USER not in text
    assert OTHER_USER not in text
    assert "Not yours" not in text and "Someone else's memory" not in text


async def test_left_behind_usage_violations_daemons_and_configuration(
    db: Any, store: Any, seeded: Seeded
) -> None:
    data, _ = await _export(db, _sources(store, None))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    for table in (
        "usage_tracking",
        "voice_usage",
        "prompt_inspection_violations",
        "daemon_registry",
        "user_identities",
        "user_file_usage",
        "instance_settings",
        "user_preferences",
    ):
        assert f"data/{table}.jsonl" not in names
    text = _text_of(data)
    for configuration in ("notes-server", "mcp.example.com", "host.example.com", "ignore previous"):
        assert configuration not in text


async def test_only_user_preferences_are_written(db: Any, store: Any, seeded: Seeded) -> None:
    sources = _sources(
        store,
        None,
        preferences={
            "interface.theme": "dark",
            "providers.anthropic.api_key": "not-a-real-key-dev-only",
            "mcp.servers": [{"name": "x", "transport": "stdio"}],
            "files.root": "/srv/files",
        },
    )
    data, _ = await _export(db, sources)
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        assert bundle.preferences() == {"interface.theme": "dark"}
    assert "not-a-real-key-dev-only" not in _text_of(data)


async def test_credentials_and_instance_secrets_are_redacted_in_text(
    db: Any, store: Any, seeded: Seeded
) -> None:
    chat = seeded.conversations[0]
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, user_guid) VALUES (?, ?, ?, ?)",
        (chat, "user", f"my key {CREDENTIAL} and {SECRET_VALUE}", USER),
    )
    await db.execute(
        "UPDATE autonomous_actions SET action_prompt = ? WHERE user_guid = ?",
        (f"Use {SECRET_VALUE} to search", USER),
    )
    sources = _sources(
        store, None, preferences={"conversation.global_instructions": f"key {CREDENTIAL}"}
    )
    data, report = await _export(db, sources)
    text = _text_of(data)
    assert CREDENTIAL not in text and SECRET_VALUE not in text
    assert report.redactions == 4
    assert secrets.REDACTED in text
    # The redacted bundle passes the import scan.
    with open_bundle(io.BytesIO(data), Limits()):
        pass


async def test_a_file_holding_a_credential_is_left_out_and_listed(
    db: Any, store: Any, seeded: Seeded
) -> None:
    store.put(USER, "documents/keys.txt", f"token {CREDENTIAL}\n".encode())
    await db.execute(
        "INSERT INTO user_files (user_guid, storage_key, filename, size_bytes) VALUES (?, ?, ?, ?)",
        (USER, "documents/keys.txt", "keys.txt", 40),
    )
    store.put(USER, "documents/secret.txt", f"value {SECRET_VALUE}\n".encode())
    await db.execute(
        "INSERT INTO user_files (user_guid, storage_key, filename, size_bytes) VALUES (?, ?, ?, ?)",
        (USER, "documents/secret.txt", "secret.txt", 40),
    )
    data, report = await _export(db, _sources(store, None))
    assert sorted(report.files_left_out) == [
        "documents/keys.txt (holds a credential)",
        "documents/secret.txt (holds a credential)",
    ]
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        keys = {r["storage_key"] for r in bundle.rows("user_files")}
    assert keys == set(seeded.files)


async def test_a_file_missing_from_the_area_is_left_out_and_listed(
    db: Any, store: Any, seeded: Seeded
) -> None:
    store.delete(USER, "documents/notes.txt")
    data, report = await _export(db, _sources(store, None))
    assert report.files_left_out == ["documents/notes.txt (missing from the file area)"]
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        assert [r["storage_key"] for r in bundle.rows("user_files")] == ["uploads/0a1b/plan.pdf"]


async def test_skill_files_are_redacted_and_unsafe_ones_left_out(
    db: Any, store: Any, skills_dir: Path, seeded: Seeded
) -> None:
    root = skills_dir / "note-taker"
    (root / "keys.md").write_text(f"use {CREDENTIAL}\n", encoding="utf-8", newline="\n")
    (root / ".hidden").write_text("x", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01" + CREDENTIAL.encode())
    (skills_dir / "Not A Skill").mkdir()
    (skills_dir / "Not A Skill" / "SKILL.md").write_text("x", encoding="utf-8")
    data, report = await _export(db, _sources(store, skills_dir))
    assert report.skills == ["note-taker"]
    assert sorted(report.skills_left_out) == [
        "Not A Skill (not a valid skill name)",
        "note-taker/.hidden (not a plain path)",
        "note-taker/image.bin (holds a credential)",
    ]
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        files = {(s, p): body for s, p, body in bundle.skill_files()}
    assert files[("note-taker", "keys.md")] == f"use {secrets.REDACTED}\n".encode()
    assert ("note-taker", "image.bin") not in files


async def test_an_empty_user_exports_a_valid_bundle(db: Any) -> None:
    data, report = await _export(db, _sources(None, None, preferences={}))
    assert report.counts == {}
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        assert list(bundle.rows("conversations")) == []
