"""create_diagram: a Mermaid diagram kept as an artefact (PC20).

The model passes a short title, the Mermaid source and an optional caption. SPARK
keeps the diagram as a file in the sender's area (kind ``diagram``, under the quota)
and the chat page draws it with Mermaid at its strict security level, with HTML labels
off. The source may not carry Mermaid directives or a configuration block. A Mermaid
block written in an ordinary reply still draws in the message, but only this tool
makes a diagram an artefact.
"""

from __future__ import annotations

from typing import Any, Final

from spark.charts.artefact import (
    DIAGRAM_LIMIT,
    DIAGRAM_MIME,
    Diagram,
    caption_text,
    diagram_document,
    diagram_source,
)
from spark.charts.validate import ChartRefused
from spark.database import files as files_db
from spark.files.store import UploadTooLarge
from spark.files.titles import TitleRefused, require_title
from spark.tools.charts import TITLE_DESCRIPTION, result_text, store_artefact, turn_context

__all__ = ["TOOL_NAME", "execute", "get_tools"]

TOOL_NAME: Final = "create_diagram"

_TOOL: Final[dict[str, Any]] = {
    "name": TOOL_NAME,
    "description": (
        "Draw a Mermaid diagram (flowchart, sequence, class, state, entity relationship, "
        "Gantt, timeline, mind map and the other Mermaid types) in the conversation and keep "
        "it as an artefact with a title. Pass only the diagram's Mermaid text: no %%{init}%% "
        "directive or config block. Every member of the conversation sees it and can open "
        "it full size or save it as PNG or SVG."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": TITLE_DESCRIPTION},
            "source": {
                "type": "string",
                "description": (
                    f"The Mermaid text of the diagram (at most {DIAGRAM_LIMIT:,} characters), "
                    "starting with its type, such as 'flowchart TD'."
                ),
            },
            "caption": {
                "type": "string",
                "description": "Optional. One or two sentences shown under the diagram.",
            },
        },
        "required": ["title", "source"],
    },
}


def get_tools() -> list[dict[str, Any]]:
    """The tool definition."""
    return [dict(_TOOL)]


def execute(tool_input: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    """Keep the diagram for the turn's sender; returns (text, is_error).

    Raises :class:`~spark.files.scope.ToolScopeUnavailable` without a user or a bridge
    and :class:`~spark.files.area.FileAreaNotConfigured` without storage.
    """
    user, db, conversation_id = turn_context(config, TOOL_NAME)
    try:
        diagram = Diagram(
            require_title(tool_input.get("title")),
            caption_text(tool_input.get("caption")),
            diagram_source(tool_input.get("source")),
        )
        card = store_artefact(
            db,
            user,
            conversation_id,
            diagram_document(diagram),
            title=diagram.title,
            kind="diagram",
            mime=DIAGRAM_MIME,
            suffix=".diagram.json",
        )
    except (ChartRefused, TitleRefused) as refused:
        return f"The diagram was not made: {refused}", True
    except (UploadTooLarge, files_db.QuotaExceeded) as exc:
        return f"The diagram was not saved: {exc}", True
    return result_text(card, "Diagram"), False
