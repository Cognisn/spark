"""A chosen model is a pair: a connection and a model id on it.

Everywhere SPARK keeps a chosen model (a conversation, a debate or panel
participant, a scheduled action, a knowledge graph build, a preference) it keeps
the pair as one text, ``<connection id>/<model id>``, so the pages, the stored
rows and the endpoints that pass a model along need no second field. A
connection id never holds ``/`` (:data:`~spark.connections.model.CONNECTION_ID_PATTERN`),
so the text splits at its first ``/`` whatever the model id holds.

A text that does not split into a valid connection id and a model id is a model
id of its own, as SPARK kept it before connections (a conversation from 0.2, say):
:meth:`spark.connections.runtime.ConnectionRuntime.resolve` finds a connection that
offers it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from spark.connections.model import CONNECTION_ID_PATTERN

__all__ = ["SEPARATOR", "ModelRef"]

SEPARATOR: Final = "/"


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A model on a connection."""

    connection_id: str
    model_id: str

    def __str__(self) -> str:
        return f"{self.connection_id}{SEPARATOR}{self.model_id}"

    @classmethod
    def parse(cls, text: str) -> ModelRef | None:
        """The pair ``text`` names, or None when it is a bare model id."""
        if not isinstance(text, str):
            return None
        connection_id, sep, model_id = text.partition(SEPARATOR)
        if not sep or not model_id or not re.fullmatch(CONNECTION_ID_PATTERN, connection_id):
            return None
        return cls(connection_id, model_id)
