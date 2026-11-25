"""The preview's routes (spark.web.endpoints.preview) on SQLite.

* ``/preview/api/<conversation>/files/<file>``: a file of the conversation for anyone
  the conversation's access decision lets read it (the owner, a contributor, a
  viewer), 404 for anyone else and for a removed member; never a file of another
  conversation; served so a browser never renders it as a page of SPARK's.
* ``/preview/frame``: the sandboxed renderer page and its Content Security Policy.

Users are switched as the personal-mode tests switch them (``app.state.user_guid``);
server mode with warden runs in test_preview_sharing_web.py.
"""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversation_members
from spark.database import conversations as convdb
from spark.database.scope import Role
from spark.mode import Mode
from spark.web.endpoints import preview
from spark.web.server import attach_database, create_app
from tests.preview_helpers import pdf_bytes
from tests.web_helpers import sign_in

ADA = "user-ada-0001"
CORA = "user-cora-0002"
VIC = "user-vic-0003"
SAM = "user-sam-0004"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture
def client(spark_db_settings: Any, tmp_path: Path) -> Iterator[TestClient]:
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "volume")}),
        data_dir=tmp_path,
        runtime_mode=Mode.PERSONAL,
    )
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    attach_database(app, spark_db_settings, True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    try:
        with TestClient(app, backend_options=options) as client:
            app.state.user_guid = ADA
            sign_in(client)
            yield client
    finally:
        files_pkg.reset()


def _as(client: TestClient, user: str) -> None:
    client.app.state.user_guid = user  # type: ignore[attr-defined]


def _db(client: TestClient) -> Any:
    return client.app.state.sync_db  # type: ignore[attr-defined]


def _save(client: TestClient, user: str, name: str, data: bytes, cid: int, mime: str) -> int:
    return files_pkg.file_area(_db(client)).save(
        user,
        f"previews/t/{name}",
        io.BytesIO(data),
        filename=name,
        mime_type=mime,
        conversation_id=cid,
        source="preview",
    )


@pytest.fixture
def shared(client: TestClient) -> dict[str, int]:
    db = _db(client)
    cid = int(db.call(convdb.create_conversation, "Plans", "stub/model", ADA))
    db.call(conversation_members.share, ADA, cid, CORA, Role.CONTRIBUTOR)
    db.call(conversation_members.share, ADA, cid, VIC, Role.VIEWER)
    other = int(db.call(convdb.create_conversation, "Other", "stub/model", ADA))
    return {
        "cid": cid,
        "other": other,
        "pdf": _save(client, ADA, "report.pdf", pdf_bytes(1), cid, "application/pdf"),
        "html": _save(client, CORA, "page.html", b"<script>alert(1)</script>", cid, "text/html"),
        "elsewhere": _save(client, ADA, "private.txt", b"mine", other, "text/plain"),
    }


def _get(client: TestClient, cid: int, fid: int, query: str = "") -> Any:
    return client.get(f"/preview/api/{cid}/files/{fid}{query}")


class TestMembers:
    @pytest.mark.parametrize("who", [ADA, CORA, VIC])
    def test_every_member_opens_the_conversations_files(
        self, client: TestClient, shared: dict[str, int], who: str
    ) -> None:
        _as(client, who)
        got = _get(client, shared["cid"], shared["pdf"])
        assert got.status_code == 200
        assert got.content.startswith(b"%PDF-")
        assert got.headers["content-type"] == "application/pdf"
        assert got.headers["content-disposition"].startswith('inline; filename="report.pdf"')

    def test_a_stranger_gets_404(self, client: TestClient, shared: dict[str, int]) -> None:
        _as(client, SAM)
        assert _get(client, shared["cid"], shared["pdf"]).status_code == 404

    def test_a_removed_member_gets_404(self, client: TestClient, shared: dict[str, int]) -> None:
        _as(client, VIC)
        assert _get(client, shared["cid"], shared["pdf"]).status_code == 200
        _db(client).call(conversation_members.remove_member, ADA, shared["cid"], VIC)
        assert _get(client, shared["cid"], shared["pdf"]).status_code == 404

    def test_a_file_of_another_conversation_is_not_reachable_through_this_one(
        self, client: TestClient, shared: dict[str, int]
    ) -> None:
        _as(client, CORA)
        assert _get(client, shared["cid"], shared["elsewhere"]).status_code == 404
        _as(client, ADA)
        # Not even for its owner: a file is reached through its own conversation.
        assert _get(client, shared["cid"], shared["elsewhere"]).status_code == 404
        assert _get(client, shared["other"], shared["elsewhere"]).status_code == 200

    def test_an_unknown_file_is_404(self, client: TestClient, shared: dict[str, int]) -> None:
        assert _get(client, shared["cid"], 999_999).status_code == 404


class TestHeaders:
    def test_a_type_that_could_render_is_never_answered_as_itself(
        self, client: TestClient, shared: dict[str, int]
    ) -> None:
        got = _get(client, shared["cid"], shared["html"])
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-security-policy"].startswith("sandbox;")
        assert got.headers["cache-control"] == "private, no-store"

    def test_download_is_an_attachment(self, client: TestClient, shared: dict[str, int]) -> None:
        got = _get(client, shared["cid"], shared["pdf"], "?download=1")
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["content-disposition"].startswith('attachment; filename="report.pdf"')

    def test_every_answer_sandboxes_itself(
        self, client: TestClient, shared: dict[str, int]
    ) -> None:
        for fid in (shared["pdf"], shared["html"]):
            headers = _get(client, shared["cid"], fid).headers
            assert "sandbox" in headers["content-security-policy"]
            assert headers["referrer-policy"] == "no-referrer"
            assert headers["cross-origin-resource-policy"] == "same-origin"


class TestFrame:
    def test_the_frame_has_a_strict_policy(self, client: TestClient) -> None:
        got = client.get("/preview/frame")
        assert got.status_code == 200
        policy = got.headers["content-security-policy"]
        directives = dict(
            (part.split(" ", 1) + [""])[:2] for part in (p.strip() for p in policy.split(";"))
        )
        assert directives["default-src"] == "'none'"
        assert directives["connect-src"] == "'none'"
        assert directives["script-src"] == "'self' blob:"
        assert "unsafe-eval" not in policy and "unsafe-inline" not in directives["script-src"]
        assert directives["img-src"] == "blob: data:"
        assert directives["frame-ancestors"] == "'self'"
        assert directives["form-action"] == "'none'"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert policy == preview.FRAME_POLICY

    def test_the_frame_page_runs_no_inline_script(self, client: TestClient) -> None:
        html = client.get("/preview/frame").text
        assert "<script>" not in html and "onload=" not in html
        assert '<script src="/static/js/preview_frame.js"></script>' in html

    def test_the_chat_page_loads_it_sandboxed(self, client: TestClient) -> None:
        cid = int(_db(client).call(convdb.create_conversation, "Mine", "stub/model", ADA))
        page = client.get(f"/chat/{cid}").text
        assert 'id="preview-pane"' in page and f'data-conversation-id="{cid}"' in page
        assert '<script src="/static/js/preview.js"></script>' in page
