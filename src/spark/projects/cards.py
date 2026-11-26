"""Project cards: what the project tools show instead of acting (PC17, PC18).

The project tools never change a project on the model's word alone, except creating one
when the user's own message in this turn asks for it (:func:`asks_for_project` on
:func:`turn_message`). Everything else is a card, a pending action kept here on the
server, which the user accepts or turns down in the conversation:

* **Suggestion** (:func:`suggest`): a project SPARK proposes unprompted, with Create,
  Edit and Dismiss. A conversation holds one suggestion, whatever became of it, so SPARK
  suggests once and never again after Dismiss (the database refuses a second).
* **Update** (:func:`propose_update`): a before-and-after change to a project the user
  owns (name, description, instructions, this conversation's files as resources), with
  Confirm and Cancel, even when the user asked for it.
* **Move** (:func:`propose_move`): this conversation into a project (or out of one), with
  the projects service's widen or narrow report as its warning, Confirm and Cancel.

Every proposal is first run through the projects service (:mod:`spark.database.projects`)
in a transaction that is rolled back, so a card only offers what the service would do,
and the service's own refusal is the answer. Accepting (:func:`accept`) runs the service
again, for the user, with their rights at that moment, in one transaction with claiming
the card: a refusal leaves the card pending and nothing changed.

A card has an unguessable id and belongs to the turn's sender and its conversation: no
one else may act on it (:class:`~spark.database.scope.NotOwned`, as for something that
does not exist), the sender only while they may still send in the conversation, once
(:class:`CardGone`), and until it expires (:data:`CARD_LIFETIME`).

The tool's result carries the card on a line of its own (:data:`CARD_MARKER`), holding
only its id, conversation and kind; the chat page asks the cards API for the rest.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Final, TypeVar

from spark.database import files as files_db
from spark.database import projects as projects_db
from spark.database.scope import (
    CONTRIBUTABLE_CONVERSATIONS,
    READABLE_CONVERSATIONS,
    Capability,
    NotOwned,
    require_access,
)
from spark.db.engine import Connection, Executor, Row
from spark.projects.access import ProjectNeed, ProjectRefused, require_project

__all__ = [
    "CARD_LIFETIME",
    "CARD_MARKER",
    "Accepted",
    "CardGone",
    "CardKind",
    "CardStatus",
    "MoveChanged",
    "ProjectCard",
    "SuggestionLimit",
    "accept",
    "asks_for_project",
    "decline",
    "get_card",
    "now",
    "parse_line",
    "propose_move",
    "propose_update",
    "suggest",
    "suggestion_state",
    "turn_message",
]

# Begins the line of a tool result that carries a project card (JSON after it).
CARD_MARKER: Final = "[spark-project-card]"
# How long a card can be acted on.
CARD_LIFETIME: Final = timedelta(hours=24)

_TOOL_RESULTS: Final = "[TOOL_RESULTS]"
# Messages SPARK writes in the user's place: never the user asking for anything.
_SPARK_MARKERS: Final = ("[TURN CANCELLED", "[COMPACTED CONTEXT", "[EMERGENCY TRUNCATION")
_LOOKBACK: Final = 50
_REASON_LIMIT: Final = 500

# A request for a project in the user's own words: a verb of making, then "project"
# (or "sub-project") later in the same sentence.
_REQUEST: Final = re.compile(
    r"\b(?:create|make|set\s*up|setup|start|build|begin|new|organi[sz]e)\b"
    r"[^.!?\n]{0,60}?\b(?:sub-?)?projects?\b",
    re.IGNORECASE,
)


class CardKind(StrEnum):
    SUGGESTION = "suggestion"
    UPDATE = "update"
    MOVE = "move"


class CardStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    CANCELLED = "cancelled"


class SuggestionLimit(ProjectRefused):
    """The conversation has had its one suggestion."""

    def __init__(self) -> None:
        super().__init__(
            "A project has already been suggested in this conversation; SPARK suggests "
            "one only once."
        )


class CardGone(ProjectRefused):
    """The card was used, turned down or has expired."""


@dataclass(frozen=True, slots=True)
class ProjectCard:
    """One card: what it proposes (``payload``) and what became of it."""

    id: str
    user_guid: str
    conversation_id: int
    kind: CardKind
    status: CardStatus
    project_id: int | None
    payload: dict[str, Any]
    created_at: str
    expires_at: str
    decided_at: str | None

    def state(self, at: datetime) -> str:
        """``pending``, ``expired``, or what the user chose."""
        if self.status is CardStatus.PENDING and self.expires_at <= _stamp(at):
            return "expired"
        return self.status.value

    def line(self) -> str:
        """The tool result line that carries this card."""
        data = {"card": self.id, "conversation_id": self.conversation_id, "kind": self.kind.value}
        return f"{CARD_MARKER} {json.dumps(data, separators=(',', ':'))}"


class MoveChanged(ProjectRefused):
    """Who would see the conversation changed since the card was shown; ``card`` is the
    card with the new report, to be shown again."""

    def __init__(self, card: ProjectCard) -> None:
        super().__init__(
            "Who can see the conversation has changed since this was shown; check the "
            "warning again before moving it."
        )
        self.card = card


@dataclass(slots=True)
class Accepted:
    """What accepting a card did."""

    card: ProjectCard
    project_id: int | None = None
    changed: list[str] = field(default_factory=list)
    move: dict[str, Any] | None = None


def now() -> datetime:
    """The time cards are judged by (UTC)."""
    return datetime.now(timezone.utc)


def _stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_line(text: str) -> dict[str, Any] | None:
    """The card reference a tool result carries (``card``, ``conversation_id``, ``kind``),
    or None."""
    for line in str(text or "").splitlines():
        if not line.startswith(CARD_MARKER):
            continue
        try:
            data = json.loads(line[len(CARD_MARKER) :].strip())
            ref = {
                "card": str(data["card"]),
                "conversation_id": int(data["conversation_id"]),
                "kind": CardKind(data["kind"]).value,
            }
        except (ValueError, KeyError, TypeError):
            return None
        return ref
    return None


# -- The user's request ------------------------------------------------------------------


def asks_for_project(text: str | None) -> bool:
    """Whether a message, in the user's own words, asks for a project to be made."""
    return bool(text) and _REQUEST.search(str(text)) is not None


async def turn_message(db: Executor, user_guid: str, conversation_id: int) -> str | None:
    """The message ``user_guid`` sent to start the conversation's current turn, or None.

    The newest message in the conversation that a person wrote: tool results (stored in
    the user's place) are passed over, and the model's messages are never the user's.
    When that message is another member's, or one SPARK wrote (a cancelled turn's
    marker, compacted context), the turn is not ``user_guid``'s request: None. A user
    who may not read the conversation gets None.
    """
    rows = await db.fetch_all(
        f"""SELECT user_guid, content FROM messages
            WHERE conversation_id = ? AND role = 'user'
            AND conversation_id IN ({READABLE_CONVERSATIONS})
            ORDER BY id DESC LIMIT {_LOOKBACK}""",
        (conversation_id, user_guid),
    )
    for row in rows:
        content = str(row["content"] or "")
        if content.startswith(_TOOL_RESULTS):
            continue
        if row["user_guid"] != user_guid or content.startswith(_SPARK_MARKERS):
            return None
        return content
    return None


# -- Storage ----------------------------------------------------------------------------


_COLUMNS: Final = """id, user_guid, conversation_id, kind, status, project_id, payload,
    created_at, expires_at, decided_at"""


def _card(row: Row) -> ProjectCard:
    return ProjectCard(
        id=str(row["id"]),
        user_guid=str(row["user_guid"]),
        conversation_id=int(row["conversation_id"]),
        kind=CardKind(row["kind"]),
        status=CardStatus(row["status"]),
        project_id=None if row["project_id"] is None else int(row["project_id"]),
        payload=dict(json.loads(str(row["payload"]))),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        decided_at=None if row["decided_at"] is None else str(row["decided_at"]),
    )


async def get_card(db: Executor, card_id: str) -> ProjectCard | None:
    """A card by id, whoever it belongs to (unscoped: callers check its user and
    conversation, as the cards API does), or None."""
    row = await db.fetch_one(f"SELECT {_COLUMNS} FROM project_cards WHERE id = ?", (card_id,))
    return None if row is None else _card(row)


async def suggestion_state(db: Executor, conversation_id: int) -> CardStatus | None:
    """What became of the conversation's suggestion, or None when it has had none."""
    status = await db.fetch_value(
        "SELECT status FROM project_cards WHERE conversation_id = ? AND kind = 'suggestion'",
        (conversation_id,),
    )
    return None if status is None else CardStatus(str(status))


async def _store(
    conn: Executor,
    user_guid: str,
    conversation_id: int,
    kind: CardKind,
    payload: Mapping[str, Any],
    project_id: int | None,
) -> ProjectCard:
    created = now()
    card_id = secrets.token_urlsafe(24)
    await conn.execute(
        """INSERT INTO project_cards
            (id, user_guid, conversation_id, kind, status, project_id, payload, created_at,
             expires_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (
            card_id,
            user_guid,
            conversation_id,
            kind.value,
            project_id,
            json.dumps(dict(payload)),
            _stamp(created),
            _stamp(created + CARD_LIFETIME),
        ),
    )
    stored = await get_card(conn, card_id)
    assert stored is not None
    return stored


_T = TypeVar("_T")


class _Preview(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__("preview")
        self.value = value


async def _dry_run(db: Executor, work: Callable[[Connection], Awaitable[_T]]) -> _T:
    """``work``'s answer from the projects service, with everything it did rolled back."""
    try:
        async with db.transaction() as conn:
            raise _Preview(await work(conn))
    except _Preview as preview:
        value: _T = preview.value
        return value


async def _may_send(conn: Executor, user_guid: str, conversation_id: int) -> None:
    await require_access(conn, user_guid, conversation_id, Capability.CONTRIBUTE)


# -- Proposals --------------------------------------------------------------------------


def _reason(value: Any) -> str:
    return " ".join(str(value or "").split())[:_REASON_LIMIT]


async def suggest(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    *,
    name: str,
    description: str,
    instructions: str,
    reason: str,
) -> ProjectCard:
    """A suggestion card for a new top-level project. Raises :class:`SuggestionLimit`
    when the conversation has had its suggestion, and the projects service's refusal
    when it would not create this project."""
    await _dry_run(
        db,
        lambda conn: projects_db.create_project(
            conn, user_guid, name, description=description, instructions=instructions
        ),
    )
    payload = {
        "name": " ".join(str(name).split()),
        "description": str(description or "").strip(),
        "instructions": str(instructions or "").strip(),
        "reason": _reason(reason),
    }
    async with db.transaction() as conn:
        await _may_send(conn, user_guid, conversation_id)
        if await suggestion_state(conn, conversation_id) is not None:
            raise SuggestionLimit()
        try:
            return await _store(
                conn, user_guid, conversation_id, CardKind.SUGGESTION, payload, None
            )
        except Exception as exc:
            # A suggestion stored at the same moment: the database keeps one.
            if "unique" in str(exc).lower() or "ux_project_cards" in str(exc):
                raise SuggestionLimit() from exc
            raise


_FIELDS: Final = ("name", "description", "instructions")


async def propose_update(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    project_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    add_file_ids: Sequence[int] = (),
) -> ProjectCard:
    """An update card: the change to a project ``user_guid`` owns, before and after.

    Only the owner is offered one (:class:`~spark.database.scope.NotPermitted` for a
    member, :class:`~spark.database.scope.NotOwned` for anyone else). Files are the
    user's own files in this conversation, added as resources. Raises
    :class:`~spark.projects.access.ProjectRefused` for no change or a change the
    projects service refuses.
    """
    wanted = {
        key: value
        for key, value in (
            ("name", name),
            ("description", description),
            ("instructions", instructions),
        )
        if value is not None
    }
    files = list(dict.fromkeys(int(f) for f in add_file_ids))
    access = await require_project(db, user_guid, project_id, ProjectNeed.MANAGE)
    await _may_send(db, user_guid, conversation_id)
    named: list[dict[str, Any]] = []
    for file_id in files:
        row = await files_db.get_conversation_file(db, user_guid, conversation_id, file_id)
        if row is None or row["user_guid"] != user_guid:
            raise ProjectRefused(f"File {file_id} is not one of your files in this conversation.")
        named.append({"file_id": file_id, "name": str(row["title"] or row["filename"])})

    async def check(conn: Connection) -> list[str]:
        changed = await projects_db.update_project(conn, user_guid, project_id, **wanted)
        for file_id in files:
            await projects_db.add_resource(conn, user_guid, project_id, file_id)
        return changed

    changed = await _dry_run(db, check)
    if not changed and not files:
        raise ProjectRefused("The update changes nothing in the project.")
    after = await _dry_run(db, lambda conn: _after(conn, user_guid, project_id, wanted))
    payload = {
        "project": {"id": project_id, "name": str(access.project["name"])},
        "before": {k: str(access.project[k] or "") for k in changed},
        "after": {k: after[k] for k in changed},
        "files": named,
    }
    async with db.transaction() as conn:
        return await _store(conn, user_guid, conversation_id, CardKind.UPDATE, payload, project_id)


async def _after(
    conn: Connection, user_guid: str, project_id: int, wanted: Mapping[str, str]
) -> dict[str, str]:
    """The project's fields as the service would store them."""
    await projects_db.update_project(conn, user_guid, project_id, **wanted)
    row = await conn.fetch_one(
        "SELECT name, description, instructions FROM projects WHERE id = ?", (project_id,)
    )
    assert row is not None
    return {k: str(row[k] or "") for k in _FIELDS}


async def _project_ref(db: Executor, project_id: int | None) -> dict[str, Any] | None:
    if project_id is None:
        return None
    name = await db.fetch_value("SELECT name FROM projects WHERE id = ?", (project_id,))
    return {"id": project_id, "name": str(name or "")}


async def _move_report(
    db: Executor, user_guid: str, conversation_id: int, project_id: int | None
) -> dict[str, Any]:
    report = await _dry_run(
        db,
        lambda conn: projects_db.move_conversation(
            conn, user_guid, conversation_id, project_id, confirm=True
        ),
    )
    return {
        "from": await _project_ref(db, report.from_project),
        "to": await _project_ref(db, report.to_project),
        "widened": dict(report.widened),
        "narrowed": dict(report.narrowed),
        "shares_removed": list(report.shares_removed),
    }


_REPORT_KEYS: Final = ("widened", "narrowed", "shares_removed")


async def propose_move(
    db: Executor, user_guid: str, conversation_id: int, project_id: int | None
) -> ProjectCard:
    """A move card: this conversation into ``project_id`` (None: out of its project), with
    who would gain or lose sight of it. The projects service's move rules refuse what
    they refuse (a project the user does not own, a conversation they did not start)."""
    await _may_send(db, user_guid, conversation_id)
    payload = await _move_report(db, user_guid, conversation_id, project_id)
    async with db.transaction() as conn:
        return await _store(conn, user_guid, conversation_id, CardKind.MOVE, payload, project_id)


# -- Acting on a card -------------------------------------------------------------------


async def _claim(conn: Connection, user_guid: str, card_id: str, status: CardStatus) -> ProjectCard:
    """Mark the user's pending card ``status``; raises :class:`NotOwned` for a card that
    is not theirs (or no card) and :class:`CardGone` for one already decided or expired."""
    card = await get_card(conn, card_id)
    if card is None or card.user_guid != user_guid:
        raise NotOwned(f"card {card_id} not found")
    # The sender acts only while they may still send in the conversation.
    may_send = await conn.fetch_value(
        f"SELECT 1 FROM conversations WHERE id = ? AND id IN ({CONTRIBUTABLE_CONVERSATIONS})",
        (card.conversation_id, user_guid),
    )
    if may_send is None:
        raise NotOwned(f"card {card_id} not found")
    at = _stamp(now())
    claimed = await conn.fetch_value(
        """UPDATE project_cards SET status = ?, decided_at = ?
            WHERE id = ? AND user_guid = ? AND status = 'pending' AND expires_at > ?
            RETURNING id""",
        (status.value, at, card_id, user_guid, at),
    )
    if claimed is None:
        if card.status is CardStatus.PENDING:
            raise CardGone("This card has expired; ask again.")
        raise CardGone(f"This card was already {card.status.value}.")
    fresh = await get_card(conn, card_id)
    assert fresh is not None
    return fresh


async def accept(
    db: Executor,
    user_guid: str,
    card_id: str,
    *,
    edits: Mapping[str, Any] | None = None,
) -> Accepted:
    """Do what the card proposes, for ``user_guid`` with their rights now, and mark it
    accepted, all or nothing. ``edits`` (a suggestion only: Edit) replace its name,
    description or instructions.

    Raises :class:`NotOwned`, :class:`CardGone`, :class:`MoveChanged` (nothing moved,
    the card shows the new report) and the projects service's refusals.
    """
    try:
        async with db.transaction() as conn:
            card = await _claim(conn, user_guid, card_id, CardStatus.ACCEPTED)
            done = Accepted(card)
            if card.kind is CardKind.SUGGESTION:
                fields = {k: str(card.payload.get(k) or "") for k in _FIELDS}
                for key, value in (edits or {}).items():
                    if key in _FIELDS and value is not None:
                        fields[key] = str(value)
                done.project_id = await projects_db.create_project(
                    conn,
                    user_guid,
                    fields["name"],
                    description=fields["description"],
                    instructions=fields["instructions"],
                )
                done.changed = list(_FIELDS)
                await conn.execute(
                    "UPDATE project_cards SET project_id = ? WHERE id = ?",
                    (done.project_id, card_id),
                )
            elif card.kind is CardKind.UPDATE:
                pid = int(card.payload["project"]["id"])
                done.project_id = pid
                done.changed = await projects_db.update_project(
                    conn, user_guid, pid, **dict(card.payload["after"])
                )
                for item in card.payload.get("files", []):
                    await projects_db.add_resource(conn, user_guid, pid, int(item["file_id"]))
                    done.changed.append("resources")
            else:
                done.project_id = card.project_id
                shown = {k: card.payload.get(k) for k in _REPORT_KEYS}
                now_report = await _move_report(
                    conn, user_guid, card.conversation_id, card.project_id
                )
                if {k: now_report[k] for k in _REPORT_KEYS} != shown:
                    raise _Reshow({**card.payload, **now_report})
                report = await projects_db.move_conversation(
                    conn, user_guid, card.conversation_id, card.project_id, confirm=True
                )
                done.move = report.summary()
            done.card = await get_card(conn, card_id) or card
            return done
    except _Reshow as reshow:
        await db.execute(
            "UPDATE project_cards SET payload = ? WHERE id = ? AND status = 'pending'",
            (json.dumps(reshow.payload), card_id),
        )
        refreshed = await get_card(db, card_id)
        assert refreshed is not None
        raise MoveChanged(refreshed) from None


class _Reshow(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("reshow")
        self.payload = payload


async def decline(db: Executor, user_guid: str, card_id: str) -> ProjectCard:
    """Dismiss a suggestion, or cancel an update or a move: nothing changes. Raises
    :class:`NotOwned` and :class:`CardGone` as :func:`accept` does."""
    async with db.transaction() as conn:
        card = await get_card(conn, card_id)
        status = (
            CardStatus.DISMISSED
            if card is not None and card.kind is CardKind.SUGGESTION
            else CardStatus.CANCELLED
        )
        return await _claim(conn, user_guid, card_id, status)
