"""The mapping of SPARK user guids onto warden users (``user_identities``).

SPARK's tables keep their ``user_guid`` columns; this table says which warden
user each guid belongs to, one to one. A warden user's guid is created on first
use (:func:`user_guid_for`, normally the warden user's own id). An existing guid,
such as the interim single-user guid that owns data made before warden, is bound
to a warden user with :func:`link`, which never rebinds either side.
"""

from __future__ import annotations

import uuid

from spark.db.engine import Executor

__all__ = ["IdentityConflict", "link", "user_guid_for", "warden_user_for"]


class IdentityConflict(Exception):
    """The guid or the warden user is already mapped to someone else."""


_BY_WARDEN_USER = "SELECT user_guid FROM user_identities WHERE warden_user_id = ?"
_INSERT = (
    "INSERT INTO user_identities (user_guid, warden_user_id) VALUES (?, ?) "
    "ON CONFLICT DO NOTHING RETURNING user_guid"
)
_EITHER_SIDE = (
    "SELECT user_guid, warden_user_id FROM user_identities "
    "WHERE user_guid = ? OR warden_user_id = ?"
)


async def user_guid_for(executor: Executor, warden_user_id: str) -> str:
    """The guid mapped to ``warden_user_id``, created on first use, in one transaction.

    A new mapping uses the warden user's own id as the guid, unless that guid
    already belongs to another warden user; then a fresh guid is drawn, so one
    warden user never receives another's guid (or its data).
    """
    async with executor.transaction() as conn:
        found = await conn.fetch_value(_BY_WARDEN_USER, (warden_user_id,))
        if found is not None:
            return str(found)
        for guid in (warden_user_id, f"user-{uuid.uuid4().hex}"):
            inserted = await conn.fetch_value(_INSERT, (guid, warden_user_id))
            if inserted is not None:
                return str(inserted)
            # Nothing inserted: a concurrent first use of this warden user won
            # (use its mapping), or the guid is someone else's (draw a new one).
            found = await conn.fetch_value(_BY_WARDEN_USER, (warden_user_id,))
            if found is not None:
                return str(found)
    raise IdentityConflict(f"no free guid could be found for warden user {warden_user_id}")


async def warden_user_for(executor: Executor, user_guid: str) -> str | None:
    """The warden user ``user_guid`` is mapped to, or None."""
    value = await executor.fetch_value(
        "SELECT warden_user_id FROM user_identities WHERE user_guid = ?", (user_guid,)
    )
    return None if value is None else str(value)


async def link(executor: Executor, user_guid: str, warden_user_id: str) -> None:
    """Bind an existing guid to a warden user; refuse if either is already mapped elsewhere.

    Linking a pair that is already linked is a no-op.
    """
    async with executor.transaction() as conn:
        rows = await conn.fetch_all(_EITHER_SIDE, (user_guid, warden_user_id))
        if not rows:
            inserted = await conn.fetch_value(_INSERT, (user_guid, warden_user_id))
            if inserted is not None:
                return
            rows = await conn.fetch_all(_EITHER_SIDE, (user_guid, warden_user_id))
        if [(r["user_guid"], r["warden_user_id"]) for r in rows] == [(user_guid, warden_user_id)]:
            return
        raise IdentityConflict(
            f"user {user_guid} or warden user {warden_user_id} is already mapped to another"
        )
