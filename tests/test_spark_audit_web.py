"""Server mode, through the real app: settings, downloads and exports reach warden's audit trail.

The app runs on a PostgreSQL clone at head with warden attached; users are
synthetic and signed in through warden. Each request that changes a setting,
downloads a file or exports data leaves one row in ``warden.audit_events`` whose
actor is the signed-in warden user, and whose details carry neither a secret's
value nor any content. A failing recorder never breaks a download, and a
personal app installs no audit hook at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient
from warden.audit import AuditEvent, AuditQuery, list_events, set_recorder
from warden.audit.store import AuditRow

from spark import files as files_pkg
from spark.database import conversations, messages
from spark.identity import audit as spark_audit
from spark.identity.db import warden_database
from spark.identity.permissions import ADMINISTRATOR
from spark.mode import DatabaseSettings, Dialect, Mode
from tests.identity_web_helpers import (
    another_client,
    make_user,
    server_client,
    sign_in,
    write_config,
)

pytestmark = pytest.mark.postgres

FETCH = {"Accept": "application/json"}
SECRET_VALUE = "brave-dev-only-not-a-real-key-9876"
CONTENT = b"Ada's confidential plan"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


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
def client(tmp_path: Path, server_db: DatabaseSettings) -> Iterator[TestClient]:
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "volume")}),
        data_dir=tmp_path,
        runtime_mode=Mode.SERVER,
    )
    try:
        with server_client(write_config(tmp_path / "config.yaml"), server_db) as test_client:
            from spark.core.conversation_manager import ConversationManager
            from spark.llm.context_limits import ContextLimitResolver

            app = test_client.app
            app.state.conversation_manager = ConversationManager(  # type: ignore[attr-defined]
                app.state.sync_db,  # type: ignore[attr-defined]
                MagicMock(),
                ContextLimitResolver(None),
                user_guid="process-guid",
            )
            yield test_client
    finally:
        files_pkg.reset()


def _signed_in(client: TestClient, email: str, *roles: str) -> tuple[TestClient, str, str]:
    """A browser signed in as a new user: (browser, SPARK guid, warden user id)."""
    warden_id = make_user(client, email, *roles)
    browser = another_client(client)
    assert sign_in(browser, email).status_code == 303
    guid = browser.get("/auth/session", headers=FETCH).json()["user_guid"]
    return browser, str(guid), str(warden_id)


def _rows(client: TestClient, action: str) -> list[AuditRow]:
    app: Any = client.app

    async def read() -> list[AuditRow]:
        audit = spark_audit.current()
        if audit is not None:
            await audit.drain()
        page = await list_events(warden_database(app.state.database), AuditQuery(actions=(action,)))
        return page.rows

    rows: list[AuditRow] = client.portal.call(read)  # type: ignore[attr-defined]
    return rows


def _call(client: TestClient, fn: Any, *args: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args)  # type: ignore[attr-defined]


class TestSettings:
    def test_instance_settings_and_secrets_are_audited_by_the_administrator(
        self, client: TestClient
    ) -> None:
        admin, _, warden_id = _signed_in(client, "root@example.com", ADMINISTRATOR)
        resp = admin.post(
            "/settings/api/instance",
            json={"voice.engine": "elevenlabs", "embedded_tools.web.brave_api_key": SECRET_VALUE},
        )
        assert resp.status_code == 200, resp.text
        [setting] = _rows(client, "spark.settings.instance.changed")
        assert (setting.actor, setting.resource_id) == (warden_id, "voice.engine")
        assert "elevenlabs" not in repr(setting.details)
        [secret] = _rows(client, "spark.settings.secret.changed")
        assert (secret.actor, secret.resource_id) == (warden_id, "embedded_tools.web.brave_api_key")
        assert SECRET_VALUE not in repr(secret.details)

    def test_a_preference_change_is_audited(self, client: TestClient) -> None:
        ada, guid, warden_id = _signed_in(client, "ada@example.com")
        assert (
            ada.post("/settings/api/preferences", json={"interface.theme": "light"}).status_code
            == 200
        )
        [row] = _rows(client, "spark.settings.preference.changed")
        assert (row.actor, row.resource_id) == (warden_id, "interface.theme")
        assert row.details["user_guid"] == guid


class TestDownloads:
    def test_a_file_download_is_audited_without_its_content(self, client: TestClient) -> None:
        ada, guid, warden_id = _signed_in(client, "ada@example.com")
        upload = ada.post("/files/api", files={"file": ("plan.txt", CONTENT, "text/plain")})
        fid = upload.json()["file"]["id"]
        assert ada.get(f"/files/api/{fid}").content == CONTENT
        [row] = _rows(client, "spark.file.downloaded")
        assert (row.actor, row.resource_type, row.resource_id) == (warden_id, "file", str(fid))
        assert row.outcome == "success"
        assert row.details["user_guid"] == guid
        assert "confidential" not in repr(row.details)

    def test_another_users_file_is_not_audited_as_a_download(self, client: TestClient) -> None:
        ada, _, _ = _signed_in(client, "ada@example.com")
        grace, _, _ = _signed_in(client, "grace@example.com")
        fid = ada.post("/files/api", files={"file": ("plan.txt", CONTENT, "text/plain")}).json()[
            "file"
        ]["id"]
        assert grace.get(f"/files/api/{fid}").status_code == 404
        assert _rows(client, "spark.file.downloaded") == []

    def test_a_failing_recorder_never_breaks_the_download(self, client: TestClient) -> None:
        ada, _, _ = _signed_in(client, "ada@example.com")
        fid = ada.post("/files/api", files={"file": ("plan.txt", CONTENT, "text/plain")}).json()[
            "file"
        ]["id"]

        class Failing:
            async def record(self, event: AuditEvent, *, conn: Any = None) -> int:
                raise RuntimeError("recorder down")

        previous = set_recorder(Failing())  # type: ignore[arg-type]
        try:
            resp = ada.get(f"/files/api/{fid}")
        finally:
            set_recorder(previous)
        assert resp.status_code == 200 and resp.content == CONTENT
        assert _rows(client, "spark.file.downloaded") == []


class TestExports:
    def test_a_conversation_export_is_audited(self, client: TestClient) -> None:
        ada, guid, warden_id = _signed_in(client, "ada@example.com")
        cid = _call(client, conversations.create_conversation, "Ada's notes", "model-a", guid)
        _call(client, messages.add_message, cid, "user", "Ada's secret plan", 5, guid)
        resp = ada.get(f"/chat/{cid}/api/export?format=json")
        assert resp.status_code == 200
        [row] = _rows(client, "spark.export.downloaded")
        assert (row.actor, row.resource_type, row.resource_id) == (
            warden_id,
            "conversation",
            str(cid),
        )
        assert row.details["kind"] == "conversation" and row.details["format"] == "json"
        assert "secret plan" not in repr(row.details)

    def test_another_users_conversation_export_is_not_audited(self, client: TestClient) -> None:
        _, ada_guid, _ = _signed_in(client, "ada@example.com")
        grace, _, _ = _signed_in(client, "grace@example.com")
        cid = _call(client, conversations.create_conversation, "Ada's notes", "model-a", ada_guid)
        assert grace.get(f"/chat/{cid}/api/export?format=json").status_code == 404
        assert _rows(client, "spark.export.downloaded") == []

    def test_a_memories_export_is_audited(self, client: TestClient) -> None:
        ada, _, warden_id = _signed_in(client, "ada@example.com")
        assert ada.get("/memories/api/export").status_code == 200
        [row] = _rows(client, "spark.export.downloaded")
        assert (row.actor, row.resource_type) == (warden_id, "memories")
        assert row.details["kind"] == "memories" and row.details["count"] == 0

    def test_a_skill_download_is_audited(self, client: TestClient) -> None:
        ada, _, warden_id = _signed_in(client, "ada@example.com")
        skills = ada.get("/skills/api/list").json()["skills"]
        name = next(s["name"] for s in skills if s["valid"])
        assert ada.get(f"/skills/api/download?name={name}").status_code == 200
        [row] = _rows(client, "spark.export.downloaded")
        assert (row.actor, row.resource_type, row.resource_id) == (warden_id, "skill", name)
        assert row.details["kind"] == "skill"


def test_a_personal_app_installs_no_audit_hook(spark_db_settings: Any) -> None:
    """Personal mode: the lifespan starts no warden runtime, so no hook is installed."""
    import asyncio
    import sys

    from spark.instance_settings import audit as settings_audit
    from spark.safety import untrusted
    from spark.tools import registry
    from spark.web.server import attach_database, create_app

    if spark_db_settings.dialect is not Dialect.SQLITE:
        pytest.skip("personal mode runs on SQLite")
    ctx = MagicMock()
    ctx.settings.get = lambda key, default=None, **_: default
    app = create_app(ctx, first_run=False)
    attach_database(app, spark_db_settings, True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options):
        assert spark_audit.current() is None
        assert settings_audit.transaction_hook() is None
        assert untrusted._audit_hook is None
        assert registry._refusal_hook is None
