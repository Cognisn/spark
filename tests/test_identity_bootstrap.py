"""The first administrator: ``spark identity create-admin``.

Creates the user through warden and gives them the administrator role; running
it again changes nothing. ``--link-user-guid`` binds the interim single-user
guid to that administrator, so data made before warden stays theirs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from warden.audit import set_recorder

from spark import launch
from spark.core import application
from spark.database import user_identities
from spark.database.user_identities import IdentityConflict
from spark.identity.bootstrap import create_admin, parse_create_admin
from spark.identity.permissions import ADMINISTRATOR
from spark.identity.runtime import IdentityRuntime
from spark.mode import DatabaseSettings, Dialect
from tests.test_identity_runtime import _settings
from tests.test_instance_settings_web import MemorySecrets

PASSWORD = "correct-horse-battery-dev-only"


class TestCommandLine:
    def test_launch_routes_identity_commands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(application, "run_identity", lambda args: seen.append(args) or 0)
        with pytest.raises(SystemExit) as exc:
            launch.main(["identity", "create-admin", "--email", "ada@example.com"])
        assert exc.value.code == 0
        assert seen == [["create-admin", "--email", "ada@example.com"]]

    def test_arguments(self) -> None:
        args = parse_create_admin(["--email", "ada@example.com", "--link-user-guid", "g-1"])
        assert args.email == "ada@example.com"
        assert args.link_user_guid == "g-1"
        assert parse_create_admin(["--email", "ada@example.com"]).link_user_guid is None

    def test_email_is_required(self) -> None:
        with pytest.raises(SystemExit):
            parse_create_admin([])


@pytest.fixture
def server_db(pg_server: str, _pg_template: str) -> Iterator[DatabaseSettings]:
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=f"{base}/{name}")
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def started(server_db: DatabaseSettings) -> AsyncIterator[tuple[Any, IdentityRuntime]]:
    from spark.db import Database

    previous = set_recorder(None)
    db = await Database.open(server_db)
    runtime = await IdentityRuntime.start(db, _settings(), MemorySecrets())
    try:
        yield db, runtime
    finally:
        runtime.close()
        await db.close()
        set_recorder(previous)


@pytest.mark.postgres
class TestCreateAdmin:
    async def test_creates_an_administrator(self, started: Any) -> None:
        db, runtime = started
        result = await create_admin(runtime, db, "ada@example.com", lambda: PASSWORD)
        assert result.created and result.role_assigned
        roles = await runtime.identity.roles.roles_of(uuid.UUID(result.user_id))
        assert ADMINISTRATOR in roles
        signed = await runtime.identity.sign_in("ada@example.com", PASSWORD)
        assert signed.tokens is not None or signed.challenge is not None

    async def test_running_twice_changes_nothing(self, started: Any) -> None:
        db, runtime = started
        first = await create_admin(runtime, db, "ada@example.com", lambda: PASSWORD)
        asked: list[str] = []

        def password() -> str:
            asked.append("asked")
            return PASSWORD

        second = await create_admin(runtime, db, "ADA@example.com", password)
        assert second.user_id == first.user_id
        assert not second.created and not second.role_assigned
        assert asked == []  # an existing user's password is never asked for or changed

    async def test_links_the_interim_guid(self, started: Any) -> None:
        db, runtime = started
        result = await create_admin(
            runtime, db, "ada@example.com", lambda: PASSWORD, link_user_guid="interim-guid"
        )
        assert result.linked_user_guid == "interim-guid"
        assert await user_identities.warden_user_for(db, "interim-guid") == result.user_id
        assert await user_identities.user_guid_for(db, result.user_id) == "interim-guid"
        again = await create_admin(
            runtime, db, "ada@example.com", lambda: PASSWORD, link_user_guid="interim-guid"
        )
        assert again.linked_user_guid == "interim-guid"

    async def test_a_guid_of_someone_else_is_refused(self, started: Any) -> None:
        db, runtime = started
        await user_identities.link(db, "interim-guid", str(uuid.uuid4()))
        with pytest.raises(IdentityConflict):
            await create_admin(
                runtime, db, "ada@example.com", lambda: PASSWORD, link_user_guid="interim-guid"
            )
