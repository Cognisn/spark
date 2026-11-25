"""Artefact titles and kinds: what a file in a user's area is called, and what it is.

Every file in a user's area has a short title (the artefacts panel shows it, with the
file name and type beneath) and a kind (PC5). A title comes from text a model or a
person wrote, so it is neutralised as text before it is stored or shown anywhere:

* control and format characters (line breaks, NUL, bidirectional overrides) become
  spaces and runs of white space become one, so a title is always one line and can
  never start a line of its own in a tool result (where a card line could be forged);
* ``<`` and ``>`` become their full-width forms, so no title is ever markup, wherever
  it ends up;
* it is at most :data:`TITLE_LIMIT` characters.

The kinds are :data:`KINDS`. A file recorded without a kind takes one from its source
(:func:`kind_for_source`); without a title it is called by its file name.
"""

from __future__ import annotations

import unicodedata
from typing import Final

__all__ = [
    "KINDS",
    "TITLE_LIMIT",
    "TitleRefused",
    "clean_text",
    "kind_for_source",
    "require_title",
]

# document: made by a create tool; chart and diagram: create_chart and create_diagram;
# preview: fetched for the document preview; attachment: provided by a person.
KINDS: Final = ("document", "chart", "diagram", "preview", "attachment")
TITLE_LIMIT: Final = 120

_BY_SOURCE: Final[dict[str, str]] = {
    "upload": "attachment",
    "import": "attachment",
    "preview": "preview",
    "created": "document",
}
_MARKUP: Final = str.maketrans({"<": "＜", ">": "＞"})


class TitleRefused(ValueError):
    """A title that cannot be used; the message says what is needed."""


def clean_text(value: object, *, limit: int) -> str:
    """``value`` as one line of plain text: no control or format characters, no markup
    characters, white space collapsed, at most ``limit`` characters."""
    text = "" if value is None else str(value)
    spaced = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text
    )
    return " ".join(spaced.split()).translate(_MARKUP)[:limit].strip()


def require_title(value: object, *, what: str = "title") -> str:
    """A cleaned title, refusing one that is missing, blank or too long."""
    if not isinstance(value, str) or not clean_text(value, limit=TITLE_LIMIT + 1):
        raise TitleRefused(
            f"A {what} is required: a short name for it, such as 'Quarterly sales by region'."
        )
    cleaned = clean_text(value, limit=TITLE_LIMIT + 1)
    if len(cleaned) > TITLE_LIMIT:
        raise TitleRefused(
            f"The {what} is longer than {TITLE_LIMIT} characters; give a shorter one."
        )
    return cleaned


def kind_for_source(source: str) -> str:
    """The kind of a file recorded from ``source`` without one of its own."""
    return _BY_SOURCE.get(source, "document")
