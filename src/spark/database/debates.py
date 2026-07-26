"""Debate persistence: config, agents, turns, and exhibits."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

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


def create_debate(
    db: DatabaseConnection,
    conversation_id: int,
    topic: str,
    rounds_mode: str,
    max_rounds: int | None,
    user_guid: str,
    agents: dict[str, dict[str, Any]],
) -> int:
    """Create the debate config and its three agent rows. Returns config ID."""
    ph = db.placeholder
    cur = db.execute(
        f"""INSERT INTO debate_config
            (conversation_id, topic, rounds_mode, max_rounds, state, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, 'setup', {ph})""",
        (conversation_id, topic, rounds_mode, max_rounds, user_guid),
    )
    config_id = cur.lastrowid
    import json as json_mod

    for role, spec in agents.items():

        def _allow(key: str, spec: dict[str, Any] = spec) -> str | None:
            value = spec.get(key)
            return json_mod.dumps(value) if isinstance(value, list) else None

        db.execute(
            f"""INSERT INTO debate_agents
                (conversation_id, role, model_id, brief, allowed_tools,
                 allowed_skills, display_name, is_human, voice_id)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
            (
                conversation_id,
                role,
                spec["model_id"],
                spec.get("brief"),
                _allow("allowed_tools"),
                _allow("allowed_skills"),
                spec.get("display_name"),
                1 if spec.get("is_human") else 0,
                spec.get("voice_id"),
            ),
        )
    db.commit()
    return config_id


def get_debate(db: DatabaseConnection, conversation_id: int) -> dict[str, Any] | None:
    """Return the debate config with its agents, or None."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT id, conversation_id, topic, rounds_mode, max_rounds, state,
                   current_round, opening_speaker, user_guid
            FROM debate_config WHERE conversation_id = {ph}""",
        (conversation_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cfg = {
        "id": row[0],
        "conversation_id": row[1],
        "topic": row[2],
        "rounds_mode": row[3],
        "max_rounds": row[4],
        "state": row[5],
        "current_round": row[6],
        "opening_speaker": row[7],
        "user_guid": row[8],
        "agents": {},
    }
    cur = db.execute(
        f"""SELECT role, model_id, brief, tokens_sent, tokens_received,
                   allowed_tools, allowed_skills, display_name, is_human, voice_id
            FROM debate_agents WHERE conversation_id = {ph}""",
        (conversation_id,),
    )

    def _parse_allow(raw: Any) -> list[str] | None:
        if raw is None:
            return None
        import json as json_mod
        import logging

        try:
            value = json_mod.loads(raw)
            return value if isinstance(value, list) else None
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning("Invalid allowlist JSON, treating as all")
            return None

    for (
        role,
        model_id,
        brief,
        sent,
        received,
        tools_raw,
        skills_raw,
        display_name,
        is_human,
        voice_id,
    ) in cur.fetchall():
        cfg["agents"][role] = {
            "model_id": model_id,
            "brief": brief,
            "tokens_sent": sent,
            "tokens_received": received,
            "allowed_tools": _parse_allow(tools_raw),
            "allowed_skills": _parse_allow(skills_raw),
            "display_name": display_name,
            "is_human": bool(is_human),
            "voice_id": voice_id,
        }
    return cfg


def update_debate_state(
    db: DatabaseConnection,
    conversation_id: int,
    state: str,
    *,
    current_round: int | None = None,
    opening_speaker: str | None = None,
) -> None:
    """Persist a state-machine transition."""
    ph = db.placeholder
    sets = ["state = " + ph]
    params: list[Any] = [state]
    if current_round is not None:
        sets.append("current_round = " + ph)
        params.append(current_round)
    if opening_speaker is not None:
        sets.append("opening_speaker = " + ph)
        params.append(opening_speaker)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(conversation_id)
    db.execute(
        f"UPDATE debate_config SET {', '.join(sets)} WHERE conversation_id = {ph}",
        tuple(params),
    )
    db.commit()


def add_turn(
    db: DatabaseConnection,
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
    ph = db.placeholder
    cur = db.execute(
        f"""INSERT INTO debate_turns
            (conversation_id, round, role, turn_type, content, status, token_count)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (conversation_id, round_no, role, turn_type, content, status, token_count),
    )
    db.commit()
    return cur.lastrowid


def update_turn(
    db: DatabaseConnection,
    turn_id: int,
    *,
    content: str | None = None,
    status: str | None = None,
    summary: str | None = None,
) -> None:
    """Update selected fields on a turn."""
    ph = db.placeholder
    sets, params = [], []
    for col, val in (("content", content), ("status", status), ("summary", summary)):
        if val is not None:
            sets.append(f"{col} = {ph}")
            params.append(val)
    if not sets:
        return
    params.append(turn_id)
    db.execute(f"UPDATE debate_turns SET {', '.join(sets)} WHERE id = {ph}", tuple(params))
    db.commit()


def get_turns(db: DatabaseConnection, conversation_id: int) -> list[dict[str, Any]]:
    """All turns for a conversation, oldest first."""
    ph = db.placeholder
    # "round" is quoted because it is a reserved word on some backends (MySQL).
    select_cols = ", ".join(f'"{c}"' if c == "round" else c for c in _TURN_COLUMNS)
    cur = db.execute(
        f"""SELECT {select_cols}
            FROM debate_turns WHERE conversation_id = {ph} ORDER BY id""",
        (conversation_id,),
    )
    return [dict(zip(_TURN_COLUMNS, row)) for row in cur.fetchall()]


def add_exhibits(db: DatabaseConnection, turn_id: int, exhibits: list[dict[str, Any]]) -> None:
    """Attach exhibits to a turn."""
    ph = db.placeholder
    for ex in exhibits:
        db.execute(
            f"""INSERT INTO debate_exhibits (turn_id, label, title, content, source)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph})""",
            (
                turn_id,
                ex.get("label", ""),
                ex.get("title"),
                ex.get("content"),
                ex.get("source"),
            ),
        )
    db.commit()


def get_exhibits(db: DatabaseConnection, conversation_id: int) -> dict[int, list[dict[str, Any]]]:
    """Exhibits for all turns of a conversation, keyed by turn ID."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT e.turn_id, e.label, e.title, e.content, e.source
            FROM debate_exhibits e
            JOIN debate_turns t ON t.id = e.turn_id
            WHERE t.conversation_id = {ph} ORDER BY e.id""",
        (conversation_id,),
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for turn_id, label, title, content, source in cur.fetchall():
        out.setdefault(turn_id, []).append(
            {"label": label, "title": title, "content": content, "source": source}
        )
    return out


def add_agent_tokens(
    db: DatabaseConnection, conversation_id: int, role: str, sent: int, received: int
) -> None:
    """Accumulate token usage on a debate agent."""
    ph = db.placeholder
    db.execute(
        f"""UPDATE debate_agents
            SET tokens_sent = tokens_sent + {ph}, tokens_received = tokens_received + {ph}
            WHERE conversation_id = {ph} AND role = {ph}""",
        (sent, received, conversation_id, role),
    )
    db.commit()
