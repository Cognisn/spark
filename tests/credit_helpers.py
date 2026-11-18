"""Test helpers for credit metering: a metered connection, allowances and spending.

At :data:`TOKENS_AS_CREDITS` rates one credit is one token of any kind (base rate 1,
every weight 1), so a test can reason in tokens. Every helper takes the executor
first, so synchronous tests call them through the bridge
(``sync_db.call(metered, ...)``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from spark import mode
from spark.database import connections as conn_repo
from spark.database import usage
from spark.database.connections import Assignment, ConnectionRow, Metering
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

TOKENS_AS_CREDITS: dict[str, Any] = {
    "base_rate_tokens": 1,
    "input_weight": 1.0,
    "output_weight": 1.0,
    "cache_read_weight": 1.0,
    "cache_write_weight": 1.0,
}


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


async def metered(
    db: Any,
    connection_id: str = "stub",
    *,
    name: str | None = None,
    is_metered: bool = True,
    rates: dict[str, Any] | None = None,
) -> None:
    """Store ``connection_id`` (if need be) and its metering (tokens as credits by default)."""
    if await conn_repo.get_connection(db, connection_id) is None:
        await conn_repo.save_connection(
            db,
            ConnectionRow(
                connection_id,
                name or f"Stub {connection_id}",
                "stub",
                {"kind": "stub"},
                {},
            ),
        )
    settings = TOKENS_AS_CREDITS if rates is None else rates
    await conn_repo.save_metering(db, Metering(connection_id, metered=is_metered, **settings))


async def allow(
    db: Any,
    connection_id: str = "stub",
    *,
    role: str | None = None,
    user: str | None = None,
    credits: int | None = None,
) -> None:
    """Assign the connection to a role or a user with a monthly allowance (None: no limit)."""
    await conn_repo.save_assignment(
        db, Assignment(connection_id, role=role, user_guid=user, monthly_credits=credits)
    )


async def unassign(
    db: Any, connection_id: str = "stub", *, role: str | None = None, user: str | None = None
) -> None:
    await conn_repo.delete_assignment(db, connection_id, role=role, user_guid=user)


async def spent(
    db: Any,
    user_guid: str,
    tokens: int,
    *,
    connection_id: str = "stub",
    model_id: str = "m",
    at: datetime | None = None,
) -> None:
    """Record a call of ``tokens`` input tokens on the connection for ``user_guid``."""
    await usage.record_usage(
        db, None, model_id, tokens, 0, user_guid, at=at, connection_id=connection_id
    )
