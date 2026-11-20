"""``spark bootstrap`` and the Bedrock connection, on PostgreSQL (server mode).

The step's rules are proved on both dialects in ``tests/test_bootstrap_bedrock.py``;
this proves the whole run: the connection is created inside the run, audited once
through warden's recorder (attributed to bootstrap), left alone on every later run,
and created on an instance whose bootstrap completed before the setting existed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spark.bootstrap.settings import BootstrapSettingsError
from spark.connections.model import residency_record
from spark.connections.store import DatabaseConnectionStore
from spark.db import Database
from tests.fake_smtp import FakeSmtp
from tests.test_bootstrap import (  # noqa: F401 - fixtures
    Server,
    _clean_state,
    _invited,
    _one,
    _run,
    _settings,
    server,
    smtp,
)

pytestmark = pytest.mark.postgres

AU = "ap-southeast-2,ap-southeast-4"
CONNECTION = "SELECT count(*) FROM spark.connections WHERE id = 'aws-bedrock'"
EVENTS = (
    "SELECT count(*) FROM warden.audit_events"
    " WHERE resource_type = 'connection' AND resource_id = 'aws-bedrock'"
)


async def _stored(server: Server) -> Any:
    db = await Database.open(server.owner, max_size=2)
    try:
        return await DatabaseConnectionStore(db).get_connection("aws-bedrock")
    finally:
        await db.close()


async def _delete(server: Server) -> None:
    db = await Database.open(server.owner, max_size=2)
    try:
        assert await DatabaseConnectionStore(db).delete_connection("aws-bedrock")
    finally:
        await db.close()


class TestBedrockStep:
    async def test_without_a_region_nothing_is_written(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        outcome = await _run(server, _settings(smtp.port))
        assert outcome.bedrock == ""
        assert _one(server.url, CONNECTION) == 0
        assert _one(server.url, EVENTS) == 0

    async def test_a_fresh_run_creates_the_connection_once_and_audits_it(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        outcome = await _run(
            server, _settings(smtp.port, bootstrap__bedrock__region="ap-southeast-2")
        )
        assert outcome.bedrock == "created" and outcome.invited
        connection = await _stored(server)
        assert connection is not None and connection.enabled
        assert dict(connection.settings) == {
            "kind": "bedrock",
            "region": "ap-southeast-2",
            "auth": {"mode": "credential_chain"},
        }
        assert residency_record(connection.residency) == {
            "scopes": ["in_region", "local"],
            "regions": ["ap-southeast-2"],
        }
        assert _one(server.url, EVENTS) == 1
        raw = _one(
            server.url, "SELECT details FROM warden.audit_events WHERE resource_id = 'aws-bedrock'"
        )
        detail = json.loads(raw) if isinstance(raw, str) else raw
        assert detail["change"] == "created" and detail["connection_id"] == "aws-bedrock"
        assert (
            _one(
                server.url,
                "SELECT actor FROM warden.audit_events WHERE resource_id = 'aws-bedrock'",
            )
            == "spark-bootstrap"
        )

    async def test_residency_regions_are_stored(self, server: Server, smtp: FakeSmtp) -> None:
        settings = _settings(
            smtp.port,
            bootstrap__bedrock__region="ap-southeast-2",
            bootstrap__bedrock__residency_regions=AU,
            bootstrap__bedrock__name="Bedrock (Sydney)",
        )
        await _run(server, settings)
        connection = await _stored(server)
        assert connection.name == "Bedrock (Sydney)"
        assert residency_record(connection.residency) == {
            "scopes": ["cross_region", "in_region", "local"],
            "regions": ["ap-southeast-2", "ap-southeast-4"],
        }

    async def test_a_second_run_changes_nothing_and_audits_nothing(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port, bootstrap__bedrock__region="ap-southeast-2")
        await _run(server, settings)
        before = await _stored(server)
        again = await _run(server, settings)
        assert again.bedrock == "exists" and again.already_complete
        assert await _stored(server) == before
        assert _one(server.url, EVENTS) == 1

    async def test_an_instance_bootstrapped_before_the_setting_gets_the_connection(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        await _run(server, _settings(smtp.port))
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 1
        assert _one(server.url, CONNECTION) == 0
        later = await _run(
            server, _settings(smtp.port, bootstrap__bedrock__region="ap-southeast-2")
        )
        assert later.already_complete and later.bedrock == "created"
        assert _one(server.url, CONNECTION) == 1
        assert len(_invited(smtp)) == 1

    async def test_a_deleted_connection_is_not_created_again(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port, bootstrap__bedrock__region="ap-southeast-2")
        await _run(server, settings)
        await _delete(server)
        again = await _run(server, settings)
        assert again.bedrock == "declined"
        assert _one(server.url, CONNECTION) == 0
        assert _one(server.url, EVENTS) == 1

    async def test_an_administrators_changes_survive_a_run(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(smtp.port, bootstrap__bedrock__region="ap-southeast-2")
        await _run(server, settings)
        db = await Database.open(server.owner, max_size=2)
        try:
            store = DatabaseConnectionStore(db)
            existing = await store.get_connection("aws-bedrock")
            assert existing is not None
            from spark.connections.model import Connection

            edited = Connection(
                existing.id,
                "Renamed",
                {**existing.settings, "region": "eu-west-1"},
                existing.residency,
                False,
            )
            await store.save_connection(edited)
        finally:
            await db.close()
        again = await _run(server, settings)
        assert again.bedrock == "exists"
        assert await _stored(server) == edited


class TestRefusals:
    async def test_a_bad_setting_refuses_before_anything_is_written(
        self, server: Server, smtp: FakeSmtp
    ) -> None:
        settings = _settings(
            smtp.port,
            bootstrap__bedrock__region="nowhere",
            bootstrap__bedrock__residency_regions="ap-southeast-4,ap-southeast-4",
        )
        with pytest.raises(BootstrapSettingsError) as caught:
            await _run(server, settings)
        text = "\n".join(caught.value.problems)
        assert "BOOTSTRAP__BEDROCK__REGION" in text
        assert _one(server.url, "SELECT count(*) FROM spark.bootstrap") == 0
        assert _one(server.url, CONNECTION) == 0
        assert _one(server.url, EVENTS) == 0
