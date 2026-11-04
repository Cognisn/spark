"""Chat attachments in server mode: two warden users, one PostgreSQL, one file volume.

The real app with warden signs in two synthetic users. Each attaches only their
own files: the upload needs the session's CSRF token, another user's file id
answers 404 on the stream, and the turn's read_my_file acts for the turn's
user only. A role without the documents permission is never offered the tools.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversations as convdb
from spark.database import tool_permissions
from spark.database.user_identities import user_guid_for
from spark.identity.permissions import AUDITOR, USER, role_permissions
from spark.mode import DatabaseSettings, Dialect, Mode
from spark.tools.registry import PERMISSIONS_KEY, get_builtin_tools
from spark.web.security import CSRF_HEADER
from tests.identity_web_helpers import (
    MemorySecrets,
    another_client,
    make_user,
    server_client,
    sign_in,
    write_config,
)
from tests.test_chat_attachments import CONTENT, ReadsAttachments, _events

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
STREAM = {"Accept": "text/event-stream"}


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@dataclass
class Signed:
    """A signed-in browser and the SPARK guid its user acts as."""

    client: TestClient
    guid: str


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


def _signed(client: TestClient, email: str) -> Signed:
    warden_id = make_user(client, email)
    browser = client if email == ADA else another_client(client)
    assert sign_in(browser, email).status_code == 303
    sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
    return Signed(browser, sync_db.call(user_guid_for, str(warden_id)))


@pytest.fixture
def app_client(tmp_path: Path, server_db: DatabaseSettings) -> Iterator[TestClient]:
    with server_client(write_config(tmp_path / "config.yaml"), server_db, MemorySecrets()) as c:
        files_pkg.configure(
            _Settings({"files.root": str(tmp_path / "volume")}),
            data_dir=tmp_path,
            runtime_mode=Mode.SERVER,
        )
        try:
            yield c
        finally:
            files_pkg.reset()


@pytest.fixture
def ada(app_client: TestClient) -> Signed:
    return _signed(app_client, ADA)


@pytest.fixture
def grace(app_client: TestClient, ada: Signed) -> Signed:
    return _signed(app_client, GRACE)


def _conversation(user: Signed) -> int:
    cid: int = user.client.app.state.sync_db.call(  # type: ignore[attr-defined]
        convdb.create_conversation, "Synthetic", "stub-model", user.guid
    )
    return cid


def _upload(user: Signed, name: str, data: bytes, cid: int | None = None) -> Any:
    form = {"conversation_id": str(cid)} if cid is not None else {}
    return user.client.post("/files/api", files={"file": (name, data, "text/plain")}, data=form)


def _manager(client: TestClient) -> ReadsAttachments:
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver
    from spark.llm.manager import LLMManager

    stub = ReadsAttachments()
    llm = LLMManager()
    llm.register_provider(stub)  # type: ignore[arg-type]
    mgr = ConversationManager(
        client.app.state.sync_db,  # type: ignore[attr-defined]
        llm,
        ContextLimitResolver(),
        max_tool_iterations=4,
        embedded_tools_config={"embedded_tools": {}},
    )
    mgr._get_llm_service_for_model = lambda model_id: stub  # type: ignore[method-assign]
    mgr._get_vector_index = lambda conversation_id, user_guid: None  # type: ignore[method-assign]
    client.app.state.conversation_manager = mgr  # type: ignore[attr-defined]
    return stub


class TestServerAttachments:
    def test_upload_needs_the_sessions_csrf_token(self, ada: Signed) -> None:
        cid = _conversation(ada)
        token = ada.client.headers.pop(CSRF_HEADER)
        try:
            refused = _upload(ada, "a.txt", b"alpha", cid)
        finally:
            ada.client.headers[CSRF_HEADER] = token
        assert refused.status_code == 403
        assert ada.client.get("/files/api").json()["files"] == []
        assert _upload(ada, "a.txt", b"alpha", cid).status_code == 201

    def test_another_users_file_cannot_be_attached(self, ada: Signed, grace: Signed) -> None:
        _manager(ada.client)
        theirs = _upload(ada, "ada.txt", b"ada private figures")
        assert theirs.status_code == 201, theirs.text
        cid = _conversation(grace)

        resp = grace.client.get(
            f"/stream/chat?conversation_id={cid}&message=x"
            f"&attachments={theirs.json()['file']['id']}",
            headers=STREAM,
        )

        assert resp.status_code == 404
        assert "ada private" not in resp.text

    def test_each_user_reads_their_own_attachment_end_to_end(
        self, ada: Signed, grace: Signed
    ) -> None:
        stub = _manager(ada.client)
        cid = _conversation(ada)
        mine = _upload(ada, "budget.txt", CONTENT, cid).json()["file"]["id"]
        theirs = _upload(grace, "grace.txt", b"grace private figures").json()["file"]["id"]
        sync_db = ada.client.app.state.sync_db  # type: ignore[attr-defined]
        for name in ("read_my_file", "list_my_files"):
            sync_db.call(tool_permissions.set_global_tool_permission, ada.guid, name, "allowed")

        resp = ada.client.get(
            f"/stream/chat?conversation_id={cid}&message=Summarise&attachments={mine}",
            headers=STREAM,
        )

        assert resp.status_code == 200, resp.text
        completes = [d for e, d in _events(resp.text) if e == "tool_complete"]
        assert [d["tool_name"] for d in completes] == ["read_my_file"]
        assert CONTENT.decode() in completes[0]["result"]
        assert "read_my_file" in stub._seen["tools"][0]

        # The same turn, asked for grace's id, acts for ada and finds nothing.
        mgr = ada.client.app.state.conversation_manager  # type: ignore[attr-defined]
        text, is_error = mgr._call_tool(
            "read_my_file",
            {"file_id": theirs},
            conversation_id=cid,
            user_guid=ada.guid,
            permissions=role_permissions(USER),
        )
        assert is_error and "grace private" not in text


def test_a_role_without_the_documents_permission_is_not_offered_the_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from spark import mode

    monkeypatch.setattr(mode, "current_mode", lambda: Mode.SERVER)
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path)}), data_dir=tmp_path, runtime_mode=Mode.SERVER
    )
    try:
        auditor = {"embedded_tools": {}, PERMISSIONS_KEY: role_permissions(AUDITOR)}
        user = {"embedded_tools": {}, PERMISSIONS_KEY: role_permissions(USER)}
        assert not {"read_my_file", "list_my_files"} & {
            t["name"] for t in get_builtin_tools(auditor)
        }
        assert {"read_my_file", "list_my_files"} <= {t["name"] for t in get_builtin_tools(user)}
    finally:
        files_pkg.reset()
