"""The files endpoint: upload, list, download and delete, scoped to the signed-in user."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversations as convdb
from spark.mode import Mode
from spark.web.server import attach_database, create_app
from tests.web_helpers import sign_in

ALICE = "user-alice-0001"
BOB = "user-bob-0002"
MB = 1024 * 1024


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture
def file_settings(tmp_path: Path) -> Iterator[_Settings]:
    settings = _Settings({"files.root": str(tmp_path / "volume")})
    files_pkg.configure(settings, data_dir=tmp_path, runtime_mode=Mode.SERVER)
    yield settings
    files_pkg.reset()


@pytest.fixture
def client(spark_db_settings: Any, file_settings: _Settings) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    attach_database(app, spark_db_settings, True)
    # psycopg needs the Selector loop on Windows; TestClient defaults to Proactor there.
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as client:
        app.state.user_guid = ALICE
        sign_in(client)
        yield client


def _as(client: TestClient, user: str) -> None:
    client.app.state.user_guid = user  # type: ignore[attr-defined]


def _upload(client: TestClient, name: str, data: bytes, **form: Any) -> Any:
    return client.post("/files/api", files={"file": (name, data, "text/plain")}, data=form)


class TestUploadListDownload:
    def test_upload_then_list_then_download(self, client: TestClient) -> None:
        resp = _upload(client, "notes.txt", b"synthetic notes")
        assert resp.status_code == 201, resp.text
        file = resp.json()["file"]
        assert file["filename"] == "notes.txt"
        assert file["size_bytes"] == 15
        assert file["key"].startswith("uploads/") and file["key"].endswith("/notes.txt")

        listing = client.get("/files/api").json()
        assert [f["id"] for f in listing["files"]] == [file["id"]]
        assert listing["usage_bytes"] == 15
        assert listing["quota_bytes"] == 1024 * MB
        assert listing["max_upload_bytes"] == 50 * MB

        download = client.get(f"/files/api/{file['id']}")
        assert download.status_code == 200
        assert download.content == b"synthetic notes"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"

    def test_hostile_filename_is_reduced_to_a_safe_name(self, client: TestClient) -> None:
        resp = _upload(client, "..\\..\\evil:stream.txt", b"x")
        assert resp.status_code == 201, resp.text
        file = resp.json()["file"]
        assert ".." not in file["key"] and ":" not in file["key"]

    def test_upload_into_own_conversation(self, client: TestClient) -> None:
        sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
        cid = sync_db.call(convdb.create_conversation, "Synthetic", "m", ALICE)
        resp = _upload(client, "a.txt", b"x", conversation_id=str(cid))
        assert resp.status_code == 201
        assert resp.json()["file"]["conversation_id"] == cid

    def test_upload_into_another_users_conversation_is_not_found(self, client: TestClient) -> None:
        sync_db = client.app.state.sync_db  # type: ignore[attr-defined]
        cid = sync_db.call(convdb.create_conversation, "Synthetic", "m", BOB)
        assert _upload(client, "a.txt", b"x", conversation_id=str(cid)).status_code == 404

    def test_delete(self, client: TestClient) -> None:
        fid = _upload(client, "a.txt", b"12345").json()["file"]["id"]
        assert client.delete(f"/files/api/{fid}").status_code == 200
        assert client.get(f"/files/api/{fid}").status_code == 404
        assert client.get("/files/api").json()["usage_bytes"] == 0


class TestPerUserScoping:
    def test_another_users_file_is_not_found(self, client: TestClient) -> None:
        fid = _upload(client, "alice.txt", b"alice only").json()["file"]["id"]
        _as(client, BOB)
        assert client.get(f"/files/api/{fid}").status_code == 404
        assert client.delete(f"/files/api/{fid}").status_code == 404
        assert client.get("/files/api").json()["files"] == []
        _as(client, ALICE)
        assert client.get(f"/files/api/{fid}").content == b"alice only"

    def test_unknown_id_is_not_found(self, client: TestClient) -> None:
        assert client.get("/files/api/999999").status_code == 404

    def test_a_session_without_a_user_is_refused(self, client: TestClient) -> None:
        fid = _upload(client, "a.txt", b"x").json()["file"]["id"]
        _as(client, "")
        assert client.get("/files/api").status_code == 401
        assert client.get(f"/files/api/{fid}").status_code == 401
        assert client.delete(f"/files/api/{fid}").status_code == 401
        assert _upload(client, "b.txt", b"x").status_code == 401

    def test_unauthenticated_requests_are_refused(self, client: TestClient) -> None:
        client.cookies.clear()
        resp = client.get("/files/api", follow_redirects=False)
        assert resp.status_code in (302, 303, 401)


class TestLimits:
    def test_over_the_upload_limit_is_refused(
        self, client: TestClient, file_settings: _Settings
    ) -> None:
        file_settings.values["files.max_upload_mb"] = 1
        resp = _upload(client, "big.bin", b"x" * (MB + 1))
        assert resp.status_code == 413
        assert "maximum upload size" in resp.json()["error"]
        assert client.get("/files/api").json()["files"] == []

    def test_a_declared_oversize_body_is_refused_before_it_is_parsed(
        self, client: TestClient, file_settings: _Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.requests import Request

        async def no_parsing(self: Request, **_: Any) -> Any:
            raise AssertionError("the body was parsed")

        file_settings.values["files.max_upload_mb"] = 1
        monkeypatch.setattr(Request, "form", no_parsing)
        resp = _upload(client, "big.bin", b"x" * (2 * MB))
        assert resp.status_code == 413

    def test_over_quota_is_refused(self, client: TestClient, file_settings: _Settings) -> None:
        file_settings.values["files.quota_mb"] = 1
        assert _upload(client, "a.bin", b"x" * (MB - 10)).status_code == 201
        resp = _upload(client, "b.bin", b"x" * 20)
        assert resp.status_code == 507
        assert "quota" in resp.json()["error"].lower()
        assert len(client.get("/files/api").json()["files"]) == 1

    def test_server_mode_without_a_root_is_unavailable(self, client: TestClient, tmp_path) -> None:
        files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.SERVER)
        assert _upload(client, "a.txt", b"x").status_code == 503
        assert client.get("/files/api").status_code == 503

    def test_parallel_uploads_against_one_quota(
        self, client: TestClient, file_settings: _Settings
    ) -> None:
        """Twelve uploads of 100 kB at once against a quota with room for exactly five."""
        file_settings.values["files.quota_mb"] = 0.5  # 524,288 bytes
        size = 100 * 1024
        start = threading.Barrier(12)

        def upload(i: int) -> int:
            start.wait()
            return int(_upload(client, f"{i}.bin", b"x" * size).status_code)

        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(upload, range(12)))
        assert sorted(codes) == [201] * 5 + [507] * 7
        listing = client.get("/files/api").json()
        assert listing["usage_bytes"] == 5 * size
        assert sum(f["size_bytes"] for f in listing["files"]) == 5 * size
