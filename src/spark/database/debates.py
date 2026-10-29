"""Debate persistence: config, agents, turns, and exhibits."""

from __future__ import annotations

import json
import logging
from typing import Any

from spark.db.engine import Executor

__all__ = [
    "add_agent_tokens",
    "add_exhibits",
    "add_turn",
    "create_debate",
    "get_debate",
    "get_exhibits",
    "get_turns",
    "update_debate_state",
    "update_turn",
]

logger = logging.getLogger(__name__)

_TURN_COLUMNS = (
    "id",
    "conversation_id",
    "round",
    "role",
    "turn_type",
    "content",
    "summary",
    "status",
    "token_count",
    "created_at",
)


def _allow(spec: dict[str, Any], key: str) -> str | None:
    value = spec.get(key)
    return json.dumps(value) if isinstance(value, list) else None


def _parse_allow(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else None
    except (ValueError, TypeError):
        logger.warning("Invalid allowlist JSON, treating as all")
        return None


async def create_debate(
    db: Executor,
    conversation_id: int,
    topic: str,
    rounds_mode: str,
    max_rounds: int | None,
    user_guid: str,
    agents: dict[str, dict[str, Any]],
) -> int:
    """Create the debate config and its agent rows, in one transaction. Returns config ID."""
    async with db.transaction() as conn:
        config_id = await conn.fetch_value(
            """INSERT INTO debate_config
                (conversation_id, topic, rounds_mode, max_rounds, state, user_guid)
                VALUES (?, ?, ?, ?, 'setup', ?) RETURNING id""",
            (conversation_id, topic, rounds_mode, max_rounds, user_guid),
        )
        for role, spec in agents.items():
            await conn.execute(
                """INSERT INTO debate_agents
                    (conversation_id, role, model_id, brief, allowed_tools,
                     allowed_skills, display_name, is_human, voice_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    role,
                    spec["model_id"],
                    spec.get("brief"),
                    _allow(spec, "allowed_tools"),
                    _allow(spec, "allowed_skills"),
                    spec.get("display_name"),
                    1 if spec.get("is_human") else 0,
                    spec.get("voice_id"),
                ),
            )
    return int(config_id)


async def get_debate(db: Executor, conversation_id: int) -> dict[str, Any] | None:
    """Return the debate config with its agents, or None."""
    row = await db.fetch_one(
        """SELECT id, conversation_id, topic, rounds_mode, max_rounds, state,
                  current_round, opening_speaker, user_guid
           FROM debate_config WHERE conversation_id = ?""",
        (conversation_id,),
    )
    if row is None:
        return None
    cfg: dict[str, Any] = {**row, "agents": {}}
    agents = await db.fetch_all(
        """SELECT role, model_id, brief, tokens_sent, tokens_received,
                  allowed_tools, allowed_skills, display_name, is_human, voice_id
           FROM debate_agents WHERE conversation_id = ?""",
        (conversation_id,),
    )
    for agent in agents:
        cfg["agents"][agent["role"]] = {
            "model_id": agent["model_id"],
            "brief": agent["brief"],
            "tokens_sent": agent["tokens_sent"],
            "tokens_received": agent["tokens_received"],
            "allowed_tools": _parse_allow(agent["allowed_tools"]),
            "allowed_skills": _parse_allow(agent["allowed_skills"]),
            "display_name": agent["display_name"],
            "is_human": bool(agent["is_human"]),
            "voice_id": agent["voice_id"],
        }
    return cfg


async def update_debate_state(
    db: Executor,
    conversation_id: int,
    state: str,
    *,
    current_round: int | None = None,
    opening_speaker: str | None = None,
) -> None:
    """Persist a state-machine transition."""
    sets = ["state = ?"]
    params: list[Any] = [state]
    if current_round is not None:
        sets.append("current_round = ?")
        params.append(current_round)
    if opening_speaker is not None:
        sets.append("opening_speaker = ?")
        params.append(opening_speaker)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(conversation_id)
    await db.execute(
        f"UPDATE debate_config SET {', '.join(sets)} WHERE conversation_id = ?",
        tuple(params),
    )


async def add_turn(
    db: Executor,
    conversation_id: int,
    round_no: int,
    role: str,
    turn_type: str,
    content: str,
    *,
    status: str = "complete",
    token_count: int = 0,
) -> int:
    """Record a debate turn. Returns the turn ID."""
    new_id = await db.fetch_value(
        """INSERT INTO debate_turns
            (conversation_id, round, role, turn_type, content, status, token_count)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (conversation_id, round_no, role, turn_type, content, status, token_count),
    )
    return int(new_id)


async def update_turn(
    db: Executor,
    turn_id: int,
    *,
    content: str | None = None,
    status: str | None = None,
    summary: str | None = None,
) -> None:
    """Update selected fields on a turn."""
    sets: list[str] = []
    params: list[Any] = []
    for col, val in (("content", content), ("status", status), ("summary", summary)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    params.append(turn_id)
    await db.execute(f"UPDATE debate_turns SET {', '.join(sets)} WHERE id = ?", tuple(params))


async def get_turns(db: Executor, conversation_id: int) -> list[dict[str, Any]]:
    """All turns for a conversation, oldest first."""
    rows = await db.fetch_all(
        f"""SELECT {', '.join(_TURN_COLUMNS)}
            FROM debate_turns WHERE conversation_id = ? ORDER BY id""",
        (conversation_id,),
    )
    return [{c: row[c] for c in _TURN_COLUMNS} for row in rows]


async def add_exhibits(db: Executor, turn_id: int, exhibits: list[dict[str, Any]]) -> None:
    """Attach exhibits to a turn."""
    await db.execute_many(
        """INSERT INTO debate_exhibits (turn_id, label, title, content, source)
            VALUES (?, ?, ?, ?, ?)""",
        [
            (turn_id, ex.get("label", ""), ex.get("title"), ex.get("content"), ex.get("source"))
            for ex in exhibits
        ],
    )


async def get_exhibits(db: Executor, conversation_id: int) -> dict[int, list[dict[str, Any]]]:
    """Exhibits for all turns of a conversation, keyed by turn ID."""
    rows = await db.fetch_all(
        """SELECT e.turn_id, e.label, e.title, e.content, e.source
            FROM debate_exhibits e
            JOIN debate_turns t ON t.id = e.turn_id
            WHERE t.conversation_id = ? ORDER BY e.id""",
        (conversation_id,),
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["turn_id"], []).append(
            {
                "label": r["label"],
                "title": r["title"],
                "content": r["content"],
                "source": r["source"],
            }
        )
    return out


async def add_agent_tokens(
    db: Executor, conversation_id: int, role: str, sent: int, received: int
) -> None:
    """Accumulate token usage on a debate agent."""
    await db.execute(
        """UPDATE debate_agents
            SET tokens_sent = tokens_sent + ?, tokens_received = tokens_received + ?
            WHERE conversation_id = ? AND role = ?""",
        (sent, received, conversation_id, role),
    )
