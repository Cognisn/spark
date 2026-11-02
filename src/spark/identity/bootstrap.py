"""The first administrator of a server: ``spark identity create-admin``.

warden has no command that creates a user, so SPARK's own does: it creates the
user through warden with a password and gives them the ``administrator`` role.
It is safe to run again: an existing user (matched by email, ignoring case)
keeps their password, and is only given the role if they lack it.

``--link-user-guid GUID`` binds an existing SPARK guid, normally the interim
single-user guid that owns everything made before warden, to the
administrator (:func:`spark.database.user_identities.link`), so that data stays
theirs. Linking never rebinds a guid or a user that is already mapped.

The password is read from standard input when it is not a terminal (one line),
or asked for twice at a prompt. It is never echoed or logged.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from spark.database import user_identities
from spark.db.engine import Executor
from spark.identity.permissions import ADMINISTRATOR
from spark.identity.runtime import IdentityRuntime

__all__ = ["AdminResult", "create_admin", "describe", "parse_create_admin", "read_password"]

ACTOR = "spark-cli"


@dataclass(frozen=True, slots=True)
class AdminResult:
    user_id: str
    created: bool
    role_assigned: bool
    linked_user_guid: str | None


async def create_admin(
    runtime: IdentityRuntime,
    database: Executor,
    email: str,
    password: Callable[[], str],
    *,
    link_user_guid: str | None = None,
) -> AdminResult:
    """Create (or find) the administrator ``email``; ``password`` is asked for only when new."""
    identity = runtime.identity
    user = await identity.users.get_by_email(email)
    created = user is None
    if user is None:
        user = await identity.users.create(email, actor=ACTOR, password=password())
    assigned = False
    if ADMINISTRATOR not in await identity.roles.roles_of(user.id):
        await identity.roles.assign(user.id, ADMINISTRATOR, actor=ACTOR)
        assigned = True
    linked: str | None = None
    if link_user_guid:
        await user_identities.link(database, link_user_guid, str(user.id))
        linked = link_user_guid
    return AdminResult(str(user.id), created, assigned, linked)


def parse_create_admin(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="spark identity create-admin",
        description="Create the first administrator of a Spark server (safe to run again).",
    )
    parser.add_argument("--email", required=True, help="the administrator's email address")
    parser.add_argument(
        "--link-user-guid",
        default=None,
        help="bind this existing Spark user guid (the interim single user's) to the administrator",
    )
    return parser.parse_args(list(argv))


def read_password() -> str:
    """The new administrator's password: one line of stdin, or asked twice at a terminal."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password for the new administrator: ")
    if getpass.getpass("Again: ") != first:
        raise ValueError("the passwords do not match")
    return first


def describe(result: AdminResult) -> str:
    """What happened, for the command's output (never the password)."""
    parts = [
        f"administrator {uuid.UUID(result.user_id)}",
        "created" if result.created else "already existed",
    ]
    if result.role_assigned:
        parts.append("administrator role assigned")
    if result.linked_user_guid:
        parts.append(f"linked to user guid {result.linked_user_guid}")
    return ", ".join(parts)
