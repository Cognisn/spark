"""Test-only: a synthetic personal-mode user with a row in every table a bundle carries.

Everything is invented (example.com, made-up names); embeddings are fixed
pseudo-random vectors. ``seed`` also writes rows a bundle must never carry
(another user's data, usage, violations, the daemon registry) so tests can
check they stay behind.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from spark.db.engine import Executor
from spark.files.store import FileStore

OTHER_USER = "other-user-guid"
WHEN = "2026-10-01 09:00:00"
LATER = "2026-10-02 10:30:00"


def vector(seed: int, dimension: int = 384) -> bytes:
    """A fixed, L2-normalised float32 vector as bytes."""
    values = np.random.default_rng(seed).standard_normal(dimension).astype(np.float32)
    return (values / np.linalg.norm(values)).astype(np.float32).tobytes()


async def ins(db: Executor, table: str, **columns: Any) -> int:
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    new_id = await db.fetch_value(
        f"INSERT INTO {table} ({names}) VALUES ({marks}) RETURNING id", tuple(columns.values())
    )
    return int(new_id)


@dataclass
class Seeded:
    user: str
    conversations: list[int] = field(default_factory=list)
    messages: list[int] = field(default_factory=list)
    memories: list[int] = field(default_factory=list)
    actions: list[int] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)


def write_skill(skills_dir: Path, name: str = "note-taker") -> None:
    root = skills_dir / name
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Takes tidy notes for a synthetic user.\n---\n"
        "Write notes in short bullet points.\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "templates" / "meeting.txt").write_text(
        "Attendees:\nDecisions:\n", encoding="utf-8", newline="\n"
    )


async def seed(db: Executor, user: str, store: FileStore | None = None) -> Seeded:
    """Seed ``user``'s data (and other rows a bundle must leave behind)."""
    s = Seeded(user)
    chat = await ins(
        db,
        "conversations",
        name="Planning the garden",
        model_id="model-alpha",
        created_at=WHEN,
        last_updated=LATER,
        total_tokens=420,
        instructions="Answer briefly.",
        tokens_sent=300,
        tokens_received=120,
        rag_threshold=0.45,
        is_favourite=1,
        kg_local_enabled=1,
        user_guid=user,
    )
    debate = await ins(
        db,
        "conversations",
        name="Debate: tea or coffee",
        model_id="model-beta",
        created_at=WHEN,
        last_updated=WHEN,
        conversation_type="debate",
        user_guid=user,
    )
    s.conversations = [chat, debate]
    for i, (role, content) in enumerate(
        [
            ("user", "Which vegetables suit a shady bed?"),
            ("assistant", "Lettuce, spinach and kale tolerate shade."),
            ("user", "Thanks, café notes: naïve résumé ✓"),
        ]
    ):
        s.messages.append(
            await ins(
                db,
                "messages",
                conversation_id=chat,
                role=role,
                content=content,
                token_count=10 + i,
                timestamp=WHEN,
                is_rolled_up=1 if i == 0 else 0,
                user_guid=user,
            )
        )
    await ins(
        db,
        "rollup_history",
        conversation_id=chat,
        original_message_count=1,
        summarised_content="Asked about shade plants.",
        original_token_count=10,
        summarised_token_count=5,
        rollup_timestamp=WHEN,
        user_guid=user,
    )
    await ins(
        db,
        "conversation_files",
        conversation_id=chat,
        filename="plan.txt",
        file_type="text",
        file_size=12,
        content_text="bed one: kale",
        mime_type="text/plain",
        token_count=4,
        added_at=WHEN,
        tags="garden",
        user_guid=user,
    )
    await ins(
        db,
        "conversation_links",
        source_conversation_id=chat,
        target_conversation_id=debate,
        created_at=WHEN,
        user_guid=user,
    )
    await ins(
        db,
        "conversation_model_usage",
        conversation_id=chat,
        model_id="model-alpha",
        input_tokens=300,
        output_tokens=120,
        first_used=WHEN,
        last_used=LATER,
        user_guid=user,
    )
    for tool, state in (("web_search", "allowed"), ("run_command", "allowed")):
        await ins(
            db,
            "conversation_tool_permissions",
            conversation_id=chat,
            tool_name=tool,
            permission_state=state,
            granted_at=WHEN,
            updated_at=WHEN,
            user_guid=user,
        )
    for tool, enabled in (("web", 1), ("filesystem", 1), ("documents", 0)):
        await ins(
            db,
            "conversation_embedded_tools",
            conversation_id=chat,
            tool_name=tool,
            enabled=enabled,
            updated_at=WHEN,
            user_guid=user,
        )
    for server in ("local-notes", "remote-search", "retired-server"):
        await ins(
            db,
            "conversation_mcp_servers",
            conversation_id=chat,
            server_name=server,
            enabled=1,
            updated_at=WHEN,
            user_guid=user,
        )
    await ins(db, "conversation_skills", conversation_id=chat, skill_name="note-taker", enabled=1)
    await ins(
        db,
        "context_index_elements",
        conversation_id=chat,
        element_type="message",
        content_hash=hashlib.sha256(b"kale").hexdigest(),
        content_text="Lettuce, spinach and kale tolerate shade.",
        embedding=vector(1),
        metadata_json='{"message_id": 2}',
        created_at=WHEN,
        user_guid=user,
    )
    await ins(
        db,
        "mcp_transactions",
        conversation_id=chat,
        message_id=s.messages[1],
        user_prompt="Search for shade plants",
        tool_name="search",
        tool_server="remote-search",
        tool_input='{"q": "shade plants"}',
        tool_response='{"results": 3}',
        is_error=0,
        execution_time_ms=125,
        transaction_timestamp=WHEN,
        user_guid=user,
    )
    await ins(
        db,
        "agent_runs",
        agent_id="agent-0001",
        parent_conversation_id=chat,
        agent_name="researcher",
        task_description="Find shade plants",
        mode="orchestrator",
        model_id="model-alpha",
        status="completed",
        result_text="Kale.",
        tool_calls_json="[]",
        input_tokens=50,
        output_tokens=5,
        created_at=WHEN,
        completed_at=LATER,
        user_guid=user,
    )
    await ins(
        db,
        "debate_config",
        conversation_id=debate,
        topic="Tea or coffee",
        rounds_mode="fixed",
        max_rounds=2,
        state="finished",
        current_round=2,
        opening_speaker="pro",
        created_at=WHEN,
        updated_at=LATER,
        user_guid=user,
    )
    await ins(
        db,
        "debate_agents",
        conversation_id=debate,
        role="pro",
        model_id="model-beta",
        brief="Argue for tea.",
        tokens_sent=20,
        tokens_received=30,
        allowed_tools='["web_search"]',
        allowed_skills="[]",
        display_name="Tea advocate",
        is_human=0,
        voice_id="voice-1",
    )
    turn = await ins(
        db,
        "debate_turns",
        conversation_id=debate,
        round=1,
        role="pro",
        turn_type="opening",
        content="Tea is gentler.",
        summary="Tea is gentle.",
        status="complete",
        token_count=30,
        created_at=WHEN,
    )
    await ins(
        db,
        "debate_exhibits",
        turn_id=turn,
        label="E1",
        title="Caffeine table",
        content="Tea 40 mg, coffee 95 mg",
        source="example.org",
        created_at=WHEN,
    )
    for i, (content, category) in enumerate(
        [("Has a shady garden bed", "facts"), ("Prefers short answers", "preferences")]
    ):
        s.memories.append(
            await ins(
                db,
                "user_memories",
                user_guid=user,
                content=content,
                category=category,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                embedding=vector(10 + i),
                importance=0.7,
                created_at=WHEN,
                last_accessed=LATER,
                source_conversation_id=chat,
                metadata_json='{"from": "chat"}',
            )
        )
    kale = await ins(
        db,
        "kg_nodes",
        scope="global",
        name="Kale",
        name_key="kale",
        entity_type="plant",
        description="A leafy green.",
        embedding=vector(20),
        weight=3,
        user_guid=user,
        created_at=WHEN,
        updated_at=LATER,
    )
    shade = await ins(
        db,
        "kg_nodes",
        scope=f"conv:{chat}",
        name="Shade",
        name_key="shade",
        entity_type="condition",
        description="Little direct sun.",
        embedding=None,
        weight=1,
        user_guid=user,
        created_at=WHEN,
        updated_at=WHEN,
    )
    other_kale = await ins(
        db,
        "kg_nodes",
        scope=f"conv:{chat}",
        name="Kale",
        name_key="kale",
        entity_type="plant",
        description="Grows in shade.",
        embedding=vector(21),
        weight=1,
        user_guid=user,
        created_at=WHEN,
        updated_at=WHEN,
    )
    await ins(
        db,
        "kg_edges",
        scope=f"conv:{chat}",
        source_node_id=other_kale,
        target_node_id=shade,
        relation="tolerates",
        description="Kale tolerates shade.",
        weight=2,
        user_guid=user,
        created_at=WHEN,
        relation_key="tolerates",
    )
    del kale
    await ins(
        db,
        "kg_builds",
        scope=f"conv:{chat}",
        source_key=f"conv:{chat}",
        watermark=s.messages[1],
        built_at=WHEN,
        status="ok",
        user_guid=user,
    )
    await ins(
        db,
        "kg_builds",
        scope="global",
        source_key="memories",
        watermark=s.memories[0],
        built_at=WHEN,
        status="ok",
        user_guid=user,
    )
    await ins(
        db,
        "skills",
        name="note-taker",
        enabled=0,
        user_guid=user,
        created_at=WHEN,
        updated_at=WHEN,
    )
    for tool in ("web_fetch", "read_file", "notes__search"):
        await ins(
            db,
            "global_tool_permissions",
            user_guid=user,
            tool_name=tool,
            permission_state="allowed",
            granted_at=WHEN,
            updated_at=WHEN,
        )
    action = await ins(
        db,
        "autonomous_actions",
        name="Morning garden check",
        description="Weather for the beds",
        action_prompt="Summarise today's weather for a garden.",
        model_id="model-alpha",
        schedule_type="recurring",
        schedule_config='{"cron": "0 7 * * *"}',
        context_mode="fresh",
        max_failures=3,
        failure_count=1,
        is_enabled=1,
        max_tokens=2048,
        created_at=WHEN,
        last_run_at=LATER,
        next_run_at="2026-10-03 07:00:00",
        user_guid=user,
        version=2,
        locked_by="daemon-1",
        locked_at=LATER,
        updated_at=LATER,
    )
    s.actions.append(action)
    for tool, server in (
        ("web_search", None),
        ("notes__search", "local-notes"),
        ("run_command", None),
    ):
        await ins(
            db,
            "action_tool_permissions",
            action_id=action,
            tool_name=tool,
            server_name=server,
            permission_state="allowed",
            granted_at=WHEN,
            user_guid=user,
        )
    await ins(
        db,
        "action_runs",
        action_id=action,
        started_at=WHEN,
        completed_at=LATER,
        status="completed",
        result_text="Sunny.",
        result_html="<p>Sunny.</p>",
        error_message=None,
        input_tokens=40,
        output_tokens=4,
        context_snapshot="{}",
        user_guid=user,
    )
    if store is not None:
        for key, data, conversation in (
            ("uploads/0a1b/plan.pdf", b"%PDF-1.4 synthetic garden plan", chat),
            ("documents/notes.txt", "Shade bed notes, café ✓\n".encode(), None),
        ):
            store.put(user, key, data)
            s.files[key] = data
            await ins(
                db,
                "user_files",
                user_guid=user,
                storage_key=key,
                filename=key.rsplit("/", 1)[1],
                size_bytes=len(data),
                mime_type="application/pdf" if key.endswith(".pdf") else "text/plain",
                conversation_id=conversation,
                source="upload" if conversation else "created",
                created_at=WHEN,
                updated_at=WHEN,
            )
        await db.execute(
            "INSERT INTO user_file_usage (user_guid, bytes_used) VALUES (?, ?)",
            (user, sum(len(d) for d in s.files.values())),
        )
    # Rows a bundle leaves behind: another user's, usage, violations, the daemon registry.
    other = await ins(db, "conversations", name="Not yours", user_guid=OTHER_USER)
    await ins(
        db, "messages", conversation_id=other, role="user", content="private", user_guid=OTHER_USER
    )
    await ins(
        db,
        "user_memories",
        user_guid=OTHER_USER,
        content="Someone else's memory",
        category="facts",
        content_hash=hashlib.sha256(b"else").hexdigest(),
    )
    await ins(
        db,
        "usage_tracking",
        conversation_id=chat,
        model_id="model-alpha",
        region="example-region",
        input_tokens=300,
        output_tokens=120,
        cost=0.01,
        user_guid=user,
    )
    await ins(db, "voice_usage", conversation_id=chat, user_guid=user, characters=40, model_id="v")
    await ins(
        db,
        "prompt_inspection_violations",
        user_guid=user,
        conversation_id=chat,
        violation_type="injection",
        severity="low",
        prompt_snippet="ignore previous",
        detection_method="pattern",
        action_taken="warn",
        confidence_score=0.5,
    )
    await ins(
        db, "daemon_registry", daemon_id="daemon-1", hostname="host.example.com", user_guid=user
    )
    return s


PERSONAL_USER = "personal-user-guid"
SOURCE_MCP = [
    {"name": "local-notes", "transport": "stdio"},
    {"name": "remote-search", "transport": "http"},
]


async def personal_bundle(template: Path, tmp: Path) -> tuple[bytes, Seeded, Path]:
    """A seeded personal SQLite database (copied from ``template``), exported as a bundle.

    Returns the bundle, what was seeded, and the source database's path.
    """
    import io
    import shutil

    from spark.bundles.exporter import ExportSources, export_bundle
    from spark.db import Database
    from spark.files.store import MountedVolumeStore
    from spark.mode import DatabaseSettings, Dialect

    path = tmp / "source.db"
    shutil.copy(template, path)
    store = MountedVolumeStore(tmp / "source-files")
    skills = tmp / "source-skills"
    write_skill(skills)
    db = await Database.open(DatabaseSettings(Dialect.SQLITE, sqlite_path=path))
    try:
        seeded = await seed(db, PERSONAL_USER, store)
        out = io.BytesIO()
        await export_bundle(
            db,
            PERSONAL_USER,
            out,
            ExportSources(
                files=store,
                skills_dir=skills,
                preferences={"interface.theme": "light", "default_model.model_id": "model-alpha"},
                mcp_servers=SOURCE_MCP,
            ),
            spark_version="0.3.0a10",
        )
    finally:
        await db.close()
    return out.getvalue(), seeded, path
