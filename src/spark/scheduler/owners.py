"""Who a scheduled action runs as on a server, and what that owner may do now (D22, A5).

The worker runs every user's actions, each as its owner. Before each run it
asks warden, at run time, about the owner: the warden user the owner's SPARK
guid is mapped to (``user_identities``), that user's status, and the
permissions their roles grant now. An owner with no warden user, or one who is
suspended or deactivated, is refused with a reason the run record keeps.
Whether the permissions include ``spark.actions`` is the executor's decision
(:meth:`spark.scheduler.executor.ActionExecutor.execute`), which every run on a
server passes through.

Personal mode has no owners to resolve: its single local user runs every action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from warden.identity import Identity, UserStatus

from spark.database import user_identities
from spark.db.engine import Executor

__all__ = ["OwnerAccess", "OwnerResolver", "WardenOwners"]


@dataclass(frozen=True, slots=True)
class OwnerAccess:
    """An action owner's standing for one run.

    ``refusal`` is None when the run may go ahead with ``permissions``; otherwise
    it says why not, and ``permissions`` is None.
    """

    user_guid: str
    permissions: frozenset[str] | None
    refusal: str | None = None

    @classmethod
    def refused(cls, user_guid: str, reason: str) -> OwnerAccess:
        return cls(user_guid, None, reason)


class OwnerResolver(Protocol):
    """Resolves an action owner's standing (async, on the database's event loop)."""

    async def resolve(self, user_guid: str) -> OwnerAccess: ...


class WardenOwners:
    """Owners resolved through warden over SPARK's database."""

    def __init__(self, identity: Identity, database: Executor) -> None:
        self._identity = identity
        self._database = database

    async def resolve(self, user_guid: str) -> OwnerAccess:
        """The owner's current permissions, or why their actions may not run."""
        warden_id = await user_identities.warden_user_for(self._database, user_guid)
        try:
            warden_user = uuid.UUID(warden_id) if warden_id is not None else None
        except ValueError:
            warden_user = None
        if warden_user is None:
            return OwnerAccess.refused(user_guid, "the action's owner has no warden user")
        user = await self._identity.users.get(warden_user)
        if user is None:
            return OwnerAccess.refused(user_guid, "the action's owner has no warden user")
        if user.status is not UserStatus.ACTIVE:
            return OwnerAccess.refused(
                user_guid, f"the action's owner is {user.status.value} in warden"
            )
        permissions = await self._identity.permissions_of(warden_user)
        return OwnerAccess(user_guid, frozenset(permissions))
