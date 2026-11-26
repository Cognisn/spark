"""The project tools: create, suggest and update projects, and move this conversation
(PC17, PC18).

* ``create_project`` creates a project (or a sub-project of one the user owns, within the
  five levels) at once, but only when the user asked: the model sets
  ``requested_by_user`` and SPARK checks the user's own message in this turn
  (:func:`spark.projects.cards.turn_message`) asks for a project
  (:func:`spark.projects.cards.asks_for_project`). Text in a tool result, the model's own
  messages and another member's message never count, so nothing the model reads can
  turn a suggestion into a creation. Otherwise the model is told to suggest instead.
* ``suggest_project`` shows a card (Create, Edit, Dismiss) and creates nothing; SPARK
  keeps one suggestion per conversation, so none follows another or a Dismiss.
* ``update_project`` shows a before-and-after card (Confirm, Cancel) for a project the
  user owns: name, description, instructions and this conversation's files as resources.
* ``move_conversation_to_project`` shows a card with who would gain or lose sight of the
  conversation (Confirm, Cancel).

Nothing here deletes a project, shares one or changes its members. Every rule about
projects is the projects service's (:mod:`spark.database.projects`); the cards are
:mod:`spark.projects.cards`. The tools belong to the ``projects`` category
(``spark.tools.projects``) and act for the turn's sender, in the turn's conversation,
through the bridge the turn carries; outside a conversation turn (a sub-agent, a
scheduled run without one) they refuse.
"""

from __future__ import annotations

from typing import Any, Final

from spark.database import projects as projects_db
from spark.database.scope import NotOwned, NotPermitted
from spark.identity import audit
from spark.projects import cards
from spark.projects.access import ProjectRefused

__all__ = ["TOOL_NAMES", "execute", "get_tools"]

TOOL_NAMES: Final = (
    "create_project",
    "suggest_project",
    "update_project",
    "move_conversation_to_project",
)

_NAME: Final = {"type": "string", "description": "A short, specific project name."}
_DESCRIPTION: Final = {
    "type": "string",
    "description": "One or two sentences on what the project is for.",
}
_INSTRUCTIONS: Final = {
    "type": "string",
    "description": (
        "Instructions (text or Markdown) every conversation in the project receives in "
        "its system prompt."
    ),
}

_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "create_project",
        "description": (
            "Create a project when the user has asked for one in their message: SPARK "
            "creates it at once with its name, description and instructions, and reports "
            "what it made. Set requested_by_user to true only when the user's own message "
            "asks for a project; SPARK checks that message and refuses otherwise. When "
            "you think a project would help but the user did not ask, use suggest_project "
            "instead. parent_id makes a sub-project of a project the user owns (at most 5 "
            "levels). This does not move the conversation: use "
            "move_conversation_to_project, which asks the user first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _NAME,
                "description": _DESCRIPTION,
                "instructions": _INSTRUCTIONS,
                "parent_id": {
                    "type": "integer",
                    "description": "Optional. The project to put this one under.",
                },
                "requested_by_user": {
                    "type": "boolean",
                    "description": "True only when the user's message asks for a project.",
                },
            },
            "required": ["name", "requested_by_user"],
        },
    },
    {
        "name": "suggest_project",
        "description": (
            "Suggest a new project to the user when you judge one would help (recurring "
            "instructions, the same files across conversations, one subject over several "
            "chats). SPARK shows a card with Create, Edit and Dismiss; nothing is created "
            "unless the user accepts. SPARK allows one suggestion per conversation, and "
            "none after the user dismisses one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _NAME,
                "description": _DESCRIPTION,
                "instructions": _INSTRUCTIONS,
                "reason": {
                    "type": "string",
                    "description": "One sentence for the user on why a project would help.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_project",
        "description": (
            "Propose a change to a project the user owns: its name, description or "
            "instructions, or adding the user's files from this conversation as its "
            "resources. SPARK shows the user a before-and-after card and changes nothing "
            "until they confirm, even when they asked for the change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "The project to change."},
                "changes": {
                    "type": "object",
                    "description": "Only the fields to change.",
                    "properties": {
                        "name": _NAME,
                        "description": _DESCRIPTION,
                        "instructions": _INSTRUCTIONS,
                        "add_file_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Ids of the user's files in this conversation.",
                        },
                    },
                },
            },
            "required": ["project_id", "changes"],
        },
    },
    {
        "name": "move_conversation_to_project",
        "description": (
            "Propose moving this conversation into a project the user owns (or out of its "
            "project with null). SPARK shows the user a card saying who would gain or lose "
            "sight of the conversation, and moves it only when they confirm. The new "
            "project's instructions and resources apply from the next message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": ["integer", "null"],
                    "description": "The project, or null for no project.",
                },
            },
            "required": ["project_id"],
        },
    },
)


def get_tools() -> list[dict[str, Any]]:
    """The tool definitions."""
    return [dict(tool) for tool in _TOOLS]


def _context(config: dict[str, Any]) -> tuple[str, Any, int]:
    """The turn's sender, bridge and conversation, or a refusal."""
    user = config.get("_kg_user_guid") or config.get("_user_guid")
    db = config.get("_kg_db")
    conversation = config.get("_kg_conversation_id")
    if not user or db is None or not conversation:
        raise _Refused("The project tools work only in a conversation, for its signed-in user.")
    return str(user), db, int(conversation)


class _Refused(Exception):
    pass


def _id(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _Refused(f"{what} must be a project id (a whole number).")
    return value


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _refusal(exc: Exception, project_id: int | None = None) -> str:
    if isinstance(exc, NotOwned):
        return f"Project {project_id} was not found." if project_id else "That was not found."
    if isinstance(exc, NotPermitted):
        text = str(exc)
        if " may not " in text or not text:
            return "Only the project's owner can do that."
        return text
    return str(exc)


def _shown(user: str, card: cards.ProjectCard) -> None:
    audit.submit(
        audit.PROJECT_CARD_SHOWN,
        user_guid=user,
        resource_type="project_card",
        resource_id=card.id,
        details={
            "kind": card.kind.value,
            "conversation_id": card.conversation_id,
            "project_id": card.project_id,
        },
    )


def _create(user: str, db: Any, cid: int, args: dict[str, Any]) -> str:
    if args.get("requested_by_user") is not True:
        raise _Refused(
            "Nothing was created: create_project is for when the user asks for a project. "
            "To propose one, use suggest_project."
        )
    message = db.call(cards.turn_message, user, cid)
    if not cards.asks_for_project(message):
        raise _Refused(
            "Nothing was created: the user's message in this turn did not ask for a "
            "project. To propose one, use suggest_project, which asks the user first."
        )
    parent = args.get("parent_id")
    parent_id = None if parent is None else _id(parent, "parent_id")
    try:
        pid = db.call(
            projects_db.create_project,
            user,
            _text(args.get("name")),
            description=_text(args.get("description")),
            instructions=_text(args.get("instructions")),
            parent_id=parent_id,
        )
    except (NotOwned, NotPermitted, ProjectRefused) as exc:
        raise _Refused(f"Nothing was created: {_refusal(exc, parent_id)}") from exc
    row = db.fetch_one("SELECT name, depth FROM projects WHERE id = ?", (pid,))
    audit.submit(
        audit.PROJECT_CREATED_BY_TOOL,
        user_guid=user,
        resource_type="project",
        resource_id=str(pid),
        details={"conversation_id": cid, "parent_id": parent_id, "depth": row["depth"]},
    )
    where = f" under project {parent_id}" if parent_id else ""
    return (
        f'Project "{row["name"]}" created as project {pid}{where} (level {row["depth"]}), with '
        "the description and instructions given. This conversation is not in it; to move "
        "it there, use move_conversation_to_project, which asks the user first."
    )


def _suggest(user: str, db: Any, cid: int, args: dict[str, Any]) -> str:
    try:
        card = db.call(
            cards.suggest,
            user,
            cid,
            name=_text(args.get("name")),
            description=_text(args.get("description")),
            instructions=_text(args.get("instructions")),
            reason=_text(args.get("reason")),
        )
    except (NotOwned, NotPermitted, ProjectRefused) as exc:
        raise _Refused(f"No suggestion was shown: {_refusal(exc)}") from exc
    _shown(user, card)
    return (
        f"{card.line()}\nThe user sees a card suggesting the project "
        f'"{card.payload["name"]}" with Create, Edit and Dismiss. Nothing is created unless '
        "they accept it; do not suggest another project in this conversation."
    )


def _update(user: str, db: Any, cid: int, args: dict[str, Any]) -> str:
    pid = _id(args.get("project_id"), "project_id")
    changes = args.get("changes")
    if not isinstance(changes, dict):
        raise _Refused("changes must be an object with the fields to change.")
    files = changes.get("add_file_ids") or []
    if not isinstance(files, list) or not all(
        isinstance(f, int) and not isinstance(f, bool) for f in files
    ):
        raise _Refused("add_file_ids must be a list of file ids.")
    fields = {
        key: _text(changes[key])
        for key in ("name", "description", "instructions")
        if changes.get(key) is not None
    }
    try:
        card = db.call(cards.propose_update, user, cid, pid, add_file_ids=files, **fields)
    except (NotOwned, NotPermitted, ProjectRefused) as exc:
        raise _Refused(f"Nothing was proposed: {_refusal(exc, pid)}") from exc
    _shown(user, card)
    what = ", ".join([*card.payload["after"], *(["resources"] if card.payload["files"] else [])])
    return (
        f"{card.line()}\nThe user sees a before-and-after card for project {pid} ({what}). "
        "Nothing changes until they confirm it."
    )


def _move(user: str, db: Any, cid: int, args: dict[str, Any]) -> str:
    raw = args.get("project_id")
    pid = None if raw is None else _id(raw, "project_id")
    try:
        card = db.call(cards.propose_move, user, cid, pid)
    except (NotOwned, NotPermitted, ProjectRefused) as exc:
        raise _Refused(f"Nothing was proposed: {_refusal(exc, pid)}") from exc
    _shown(user, card)
    p = card.payload
    affected = len(p["widened"]) + len(p["narrowed"]) + len(p["shares_removed"])
    warning = (
        f" Its warning says who can see the conversation would change for {affected} " "person(s)."
        if affected
        else " Nobody else would gain or lose sight of it."
    )
    return (
        f"{card.line()}\nThe user sees a card to move this conversation"
        f"{' into project ' + str(pid) if pid else ' out of its project'}.{warning} "
        "Nothing moves until they confirm it."
    )


_RUN: Final = {
    "create_project": _create,
    "suggest_project": _suggest,
    "update_project": _update,
    "move_conversation_to_project": _move,
}


def execute(tool_name: str, tool_input: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    """Run a project tool for the turn's sender; returns (text, is_error)."""
    try:
        user, db, cid = _context(config)
        return _RUN[tool_name](user, db, cid, dict(tool_input or {})), False
    except _Refused as refused:
        return str(refused), True
