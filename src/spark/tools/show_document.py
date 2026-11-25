"""show_document: present a document in the preview pane beside the conversation (CS37).

The model passes a link one of the user's MCP servers returned (for example a KVASIR
document link) or the id of a file in the conversation. SPARK's server fetches the
link under the fetch guard (:mod:`spark.preview.guard`), keeps the document with the
conversation (:mod:`spark.preview.service`), and the chat page opens it in the
preview pane for everyone viewing the conversation. A source that is not allowed is
refused with a plain reason.

The tool belongs to the documents category, so a server offers and runs it only for
a user holding ``spark.tools.documents``. It acts for the turn's sender, in the
turn's conversation, through the bridge the turn carries.
"""

from __future__ import annotations

from typing import Any, Final

from spark.files.scope import ToolScopeUnavailable
from spark.preview import service

__all__ = ["TOOL_NAME", "execute", "get_tools"]

TOOL_NAME: Final = "show_document"

_TOOL: Final[dict[str, Any]] = {
    "name": TOOL_NAME,
    "description": (
        "Show a document to the user in the preview pane beside the conversation: PDF, "
        "Word, Excel and CSV, images, Markdown, text and code are shown; anything else is "
        "offered for download. Pass either url (a link to a document that one of the "
        "user's MCP servers returned, such as a document link) or file_id (a file in this "
        "conversation, or one of the user's own files from list_my_files). Every member "
        "of the conversation can open it from its card."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "A document link returned by one of the user's MCP servers.",
            },
            "file_id": {
                "type": "integer",
                "description": "A file id: a file in this conversation or one of the user's.",
            },
            "title": {
                "type": "string",
                "description": "A short title for the preview tab. Default: the file name.",
            },
        },
    },
}


def get_tools() -> list[dict[str, Any]]:
    """The tool definition."""
    return [dict(_TOOL)]


def execute(tool_input: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    """Show the document for the turn's sender; returns (text, is_error).

    Raises :class:`~spark.files.scope.ToolScopeUnavailable` without a user, a bridge
    or a conversation, and :class:`~spark.files.area.FileAreaNotConfigured` without
    storage.
    """
    from spark.tools.registry import tool_permissions

    user = config.get("_user_guid")
    db = config.get("_files_db") or config.get("_kg_db")
    conversation_id = config.get("_kg_conversation_id")
    if not user or db is None or conversation_id is None:
        raise ToolScopeUnavailable("show_document needs a signed-in user in a conversation.")
    try:
        card = service.show(
            db,
            str(user),
            int(conversation_id),
            url=tool_input.get("url"),
            file_id=tool_input.get("file_id"),
            title=tool_input.get("title"),
            permissions=tool_permissions(config),
        )
    except service.ShowRefused as refused:
        return f"The document was not shown: {refused}", True
    return service.result_text(card), False
