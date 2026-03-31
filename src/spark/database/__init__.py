"""Spark database — unified interface for all storage operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from spark.database.backends import create_backend
from spark.database.connection import DatabaseConnection
from spark.database.schema import initialise_schema

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class Database:
    """Unified database interface wrapping all storage modules.

    Usage::

        db = Database.from_settings(ctx.settings)
        # or
        db = Database(DatabaseConnection(SQLiteBackend("spark.db")))
    """

    def __init__(self, connection: DatabaseConnection) -> None:
        self._conn = connection
        initialise_schema(self._conn)

    @classmethod
    def from_settings(cls, settings: object) -> "Database":
        """Create a Database from application settings."""
        backend = create_backend(settings)
        conn = DatabaseConnection(backend)
        return cls(conn)

    @property
    def connection(self) -> DatabaseConnection:
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # -- Conversations --------------------------------------------------------

    @property
    def conversations(self) -> object:
        """Access conversation operations via spark.database.conversations module."""
        from spark.database import conversations

        return conversations

    # -- Messages -------------------------------------------------------------

    @property
    def messages(self) -> object:
        """Access message operations via spark.database.messages module."""
        from spark.database import messages

        return messages

    # -- Files ----------------------------------------------------------------

    @property
    def files(self) -> object:
        """Access file operations via spark.database.files module."""
        from spark.database import files

        return files

    # -- Memories -------------------------------------------------------------

    @property
    def memories(self) -> object:
        """Access memory operations via spark.database.memories module."""
        from spark.database import memories

        return memories

    # -- Tool Permissions -----------------------------------------------------

    @property
    def tool_permissions(self) -> object:
        """Access tool permission operations via spark.database.tool_permissions module."""
        from spark.database import tool_permissions

        return tool_permissions

    # -- Usage ----------------------------------------------------------------

    @property
    def usage(self) -> object:
        """Access usage tracking via spark.database.usage module."""
        from spark.database import usage

        return usage

    # -- Context Index --------------------------------------------------------

    @property
    def context_index(self) -> object:
        """Access context index via spark.database.context_index module."""
        from spark.database import context_index

        return context_index

    # -- MCP Operations -------------------------------------------------------

    @property
    def mcp_ops(self) -> object:
        """Access MCP operations via spark.database.mcp_ops module."""
        from spark.database import mcp_ops

        return mcp_ops

    # -- Autonomous Actions ---------------------------------------------------

    @property
    def autonomous_actions(self) -> object:
        """Access autonomous actions via spark.database.autonomous_actions module."""
        from spark.database import autonomous_actions

        return autonomous_actions
