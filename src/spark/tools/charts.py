"""create_chart: a chart the model defines and SPARK draws (PC19, PC20).

The model passes a short title, a SPARK chart definition (:mod:`spark.charts.schema`)
and an optional caption. SPARK checks the definition, reads a file's data when the
definition names one (a file of this conversation or one of the sender's own), and
stores the chart, its definition with a snapshot of the data it uses, as a file in
the sender's area (kind ``chart``, under the quota). The result names the chart and
its file id, then carries its card on a line of its own (the document preview's card
line, kind ``chart``), which the chat page draws as the chart itself.

A definition SPARK cannot draw is refused with a message that says what to change.
The tool belongs to the documents category.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from spark import files as files_pkg
from spark.charts.artefact import CHART_MIME, chart_document, new_chart
from spark.charts.data import load_file_table
from spark.charts.schema import CHART_TYPES, DATA_FIELDS, ENCODINGS, OPTIONS
from spark.charts.validate import ChartRefused, FileData, prepare, validate
from spark.database import files as files_db
from spark.files.area import BridgeLike
from spark.files.scope import ToolScopeUnavailable
from spark.files.store import UploadTooLarge
from spark.files.titles import TitleRefused, require_title
from spark.preview.service import Card, card_line, safe_name

__all__ = ["TOOL_NAME", "execute", "get_tools", "store_artefact", "turn_context"]

TOOL_NAME: Final = "create_chart"

TITLE_DESCRIPTION: Final = (
    "Required. A short, specific title, such as 'Monthly sign-ups, 2026' (no file "
    "extension). It names the artefact in the conversation and heads its saved images."
)

_TOOL: Final[dict[str, Any]] = {
    "name": TOOL_NAME,
    "description": (
        "Draw a chart in the conversation from data: SPARK checks the definition and draws "
        "it, so never write ECharts options or code. Types: "
        f"{', '.join(CHART_TYPES)}. Give the data inline (columns and rows) or name a CSV "
        "or Excel file in this conversation by file_id. Say which columns play which part "
        "in encoding (x, y, series, size, colour, by type). Every member of the conversation "
        "sees the chart and can open it full size, save it as PNG or SVG, or download its "
        "data. Use get_tool_documentation('create_chart') for every type's encodings, "
        "options and limits."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": TITLE_DESCRIPTION},
            "definition": {
                "type": "object",
                "description": "The chart definition.",
                "properties": {
                    "type": {"type": "string", "enum": list(CHART_TYPES)},
                    "data": {
                        "type": "object",
                        "description": (
                            "Inline {columns: [names], rows: [[cells]]}, or a file of this "
                            "conversation {file_id, sheet (Excel only), columns (optional)}."
                        ),
                        "properties": {name: {} for name in DATA_FIELDS},
                    },
                    "encoding": {
                        "type": "object",
                        "description": (
                            "Column names for each part the type uses; y may list several "
                            "columns, one series each."
                        ),
                        "properties": {name: {} for name in ENCODINGS},
                    },
                    "x_title": {"type": "string", "description": "Horizontal axis title."},
                    "y_title": {"type": "string", "description": "Vertical axis title."},
                    **{
                        name: {"type": "boolean", "description": description}
                        for name, description in zip(
                            OPTIONS,
                            (
                                "pie only: draw a ring.",
                                "bar, line and area: stack the series.",
                                "bar only: horizontal bars.",
                            ),
                            strict=True,
                        )
                    },
                },
                "required": ["type", "data", "encoding"],
            },
            "caption": {
                "type": "string",
                "description": "Optional. One or two sentences shown under the chart.",
            },
        },
        "required": ["title", "definition"],
    },
}


def get_tools() -> list[dict[str, Any]]:
    """The tool definition."""
    return [dict(_TOOL)]


def turn_context(config: dict[str, Any], tool: str) -> tuple[str, BridgeLike, int | None]:
    """The turn's sender, bridge and conversation; raises :class:`ToolScopeUnavailable`."""
    user = config.get("_user_guid") or config.get("_kg_user_guid")
    db = config.get("_files_db") or config.get("_kg_db")
    if not user or db is None:
        raise ToolScopeUnavailable(f"{tool} needs a signed-in user's file area.")
    cid = config.get("_kg_conversation_id")
    return str(user), db, int(cid) if cid else None


def store_artefact(
    db: BridgeLike,
    user: str,
    conversation_id: int | None,
    raw: bytes,
    *,
    title: str,
    kind: str,
    mime: str,
    suffix: str,
) -> Card:
    """Keep ``raw`` in the sender's area as an artefact of the conversation.

    Raises :class:`~spark.files.store.UploadTooLarge` and
    :class:`~spark.database.files.QuotaExceeded`.
    """
    name = f"{safe_name(title)}{suffix}"
    area = files_pkg.file_area(db)
    file_id = area.save(
        user,
        f"{kind}s/{uuid.uuid4().hex}/{name}",
        raw,
        filename=name,
        mime_type=mime,
        conversation_id=conversation_id,
        source="created",
        title=title,
        kind=kind,
    )
    return Card(
        file_id=file_id,
        conversation_id=conversation_id or 0,
        name=name,
        title=title,
        type=mime,
        kind=kind,
        size=len(raw),
    )


def result_text(card: Card, what: str) -> str:
    """The sentence for the model, then the card line for the page (in a conversation)."""
    sentence = (
        f'{what} "{card.title}" is drawn in the conversation as file {card.file_id}; every '
        "member of the conversation can open it full size and save it from its card."
    )
    return f"{sentence}\n{card_line(card)}" if card.conversation_id else sentence


def execute(tool_input: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    """Make the chart for the turn's sender; returns (text, is_error).

    Raises :class:`~spark.files.scope.ToolScopeUnavailable` without a user or a bridge
    and :class:`~spark.files.area.FileAreaNotConfigured` without storage.
    """
    user, db, conversation_id = turn_context(config, TOOL_NAME)
    try:
        title = require_title(tool_input.get("title"))
        spec = validate(tool_input.get("definition"))
        if isinstance(spec.data, FileData):
            table = load_file_table(db, user, conversation_id, spec.data)
        else:
            table = spec.data
        prepared = prepare(spec, table)
        chart = new_chart(
            title,
            tool_input.get("caption"),
            prepared,
            spec.data if isinstance(spec.data, FileData) else None,
        )
        raw = chart_document(chart)
        card = store_artefact(
            db,
            user,
            conversation_id,
            raw,
            title=chart.title,
            kind="chart",
            mime=CHART_MIME,
            suffix=".chart.json",
        )
    except (ChartRefused, TitleRefused) as refused:
        return f"The chart was not made: {refused}", True
    except (UploadTooLarge, files_db.QuotaExceeded) as exc:
        return f"The chart was not saved: {exc}", True
    label = f"{spec.type.capitalize()} chart"
    return result_text(card, label), False
