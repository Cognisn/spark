"""Alembic environment for SPARK's chain.

Run through :mod:`spark.db.migrate`, which passes an open SQLAlchemy connection
in ``config.attributes["connection"]``. On PostgreSQL SPARK's tables and its
version table live in the ``spark`` schema; on SQLite in the default schema.
"""

from __future__ import annotations

from alembic import context

config = context.config
connection = config.attributes["connection"]
version_schema = config.attributes.get("version_schema")

context.configure(
    connection=connection,
    target_metadata=None,
    version_table_schema=version_schema,
)

with context.begin_transaction():
    context.run_migrations()
