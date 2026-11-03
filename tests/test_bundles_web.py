"""The bundle screens: personal-mode export, and the server administrator's import page (A15).

Server tests run the real app on a PostgreSQL clone with warden attached;
users are synthetic and signed in through warden. The bundle imported is one a
seeded personal SQLite database exported (tests.bundle_seed).
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from warden.audit import AuditQuery, list_events

from spark import files as files_pkg
from spark.bundles import format as bf
from spark.bundles.reader import Limits, open_bundle
from spark.db import Database
from spark.files.store import MountedVolumeStore
from spark.identity import audit as spark_audit
from spark.identity.db import warden_database
from spark.identity.permissions import ADMINISTRATOR, POWER_USER, USER
from spark.mode import DatabaseSettings, Dialect, Mode
from spark.skills import manager as skills_manager
from spark.web.endpoints import bundles as bundles_endpoint
from spark.web.server import attach_database, create_app
from tests.bundle_compare import compare
from tests.bundle_seed import PERSONAL_USER, personal_bundle, seed, write_skill
from tests.identity_screen_helpers import server_db  # noqa: F401  (fixture)
from tests.identity_web_helpers import (
    PUBLIC,
    another_client,
    make_user,
    server_client,
    sign_in,
    write_config,
)
from tests.web_helpers import sign_in as personal_sign_in

FETCH = {"Accept": "application/json"}


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


# -- Personal mode: export -----------------------------------------------------------


@pytest.fixture
def personal(tmp_path: Path, sqlite_settings: Any) -> Iterator[TestClient]:
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "files")}),
        data_dir=tmp_path,
        runtime_mode=Mode.PERSONAL,
    )
    write_skill(tmp_path / "skills")
    skills_manager.set_skills_manager(skills_manager.SkillsManager(tmp_path / "skills"))
    ctx = MagicMock()
    ctx.settings.get = lambda key, default=None, **_: {"interface.session_timeout_minutes": 60}.get(
        key, default
    )
    ctx.secrets.get = lambda name: None
    app = create_app(ctx, first_run=False)
    attach_database(app, sqlite_settings, True)
    try:
        with TestClient(app) as client:
            app.state.user_guid = PERSONAL_USER
            personal_sign_in(client)
            yield client
    finally:
        skills_manager.set_skills_manager(None)
        files_pkg.reset()


def test_export_downloads_the_users_bundle(personal: TestClient) -> None:
    app: Any = personal.app
    store = files_pkg.file_store()
    personal.portal.call(seed, app.state.database, PERSONAL_USER, store)  # type: ignore[attr-defined]
    resp = personal.get("/bundles/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    disposition = resp.headers["content-disposition"]
    assert re.match(r'attachment; filename="spark-bundle-\d{8}-[0-9a-f]{8}\.zip"', disposition)
    assert resp.headers["cache-control"] == "no-store"
    with open_bundle(io.BytesIO(resp.content), Limits()) as bundle:
        assert bundle.manifest["counts"]["conversations"] == 2
        assert bundle.manifest["counts"]["user_files"] == 2
        assert {s for s, _, _ in bundle.skill_files()} == {"note-taker"}


def test_the_settings_page_offers_the_export(personal: TestClient) -> None:
    page = personal.get("/settings")
    assert page.status_code == 200
    assert 'href="/bundles/export"' in page.text


def test_personal_mode_has_no_import_page(personal: TestClient) -> None:
    assert personal.get("/admin/bundles").status_code == 404
    assert personal.post("/admin/bundles", files={"bundle": ("b.zip", b"x")}).status_code == 404


def test_export_needs_a_signed_in_session(personal: TestClient) -> None:
    signed_out = TestClient(personal.app)
    resp = signed_out.get("/bundles/export", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)


# -- Server mode: import -------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path, server_db: DatabaseSettings) -> Iterator[TestClient]:  # noqa: F811
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "volume"), "files.quota_mb": 1}),
        data_dir=tmp_path,
        runtime_mode=Mode.SERVER,
    )
    try:
        with server_client(write_config(tmp_path / "config.yaml"), server_db) as client:
            yield client
    finally:
        files_pkg.reset()


@pytest.fixture
def bundle(_sqlite_template: Path, tmp_path: Path) -> tuple[bytes, Path]:
    data, _, source = asyncio.run(personal_bundle(_sqlite_template, tmp_path / "personal"))
    return data, source


def _signed_in(server: TestClient, email: str, *roles: str) -> tuple[TestClient, str]:
    warden_id = make_user(server, email, *roles)
    browser = another_client(server)
    assert sign_in(browser, email).status_code == 303
    return browser, str(warden_id)


def _token(page: Any) -> str:
    match = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
    assert match, page.text
    return match.group(1)


def _upload(browser: TestClient, data: bytes, user_id: str) -> Any:
    page = browser.get("/admin/bundles")
    assert page.status_code == 200, page.text
    return browser.post(
        "/admin/bundles",
        data={"user_id": user_id, "csrf_token": _token(page)},
        files={"bundle": ("spark-bundle.zip", data, "application/zip")},
        headers={"Origin": PUBLIC},
    )


def _offer_remote_search(admin: TestClient) -> None:
    """The server offers the bundle's HTTP MCP server, set as an administrator would."""
    resp = admin.post(
        "/settings/api/instance",
        json={"mcp.servers": [{"name": "remote-search", "transport": "http", "url": PUBLIC}]},
    )
    assert resp.status_code == 200, resp.text


def _events(server: TestClient) -> list[Any]:
    app: Any = server.app

    async def read() -> list[Any]:
        audit = spark_audit.current()
        if audit is not None:
            await audit.drain()
        page = await list_events(
            warden_database(app.state.database),
            AuditQuery(actions=(spark_audit.BUNDLE_IMPORTED,)),
        )
        return list(page.rows)

    rows: list[Any] = server.portal.call(read)  # type: ignore[attr-defined]
    return rows


def _guid(server: TestClient, warden_id: str) -> str:
    from spark.database.user_identities import user_guid_for

    app: Any = server.app
    guid: str = server.portal.call(user_guid_for, app.state.database, warden_id)  # type: ignore[attr-defined]
    return guid


@pytest.mark.postgres
def test_an_administrator_imports_a_bundle_and_sees_the_report(
    server: TestClient, bundle: tuple[bytes, Path]
) -> None:
    admin, admin_id = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    _offer_remote_search(admin)
    page = admin.get("/admin/bundles")
    assert "ada@example.com" in page.text
    resp = _upload(admin, bundle[0], ada_id)
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert "Imported" in text and "Morning garden check" in text
    assert "local-notes (stdio, which a server does not run)" in text
    assert "imported disabled" in text.lower()
    guid = _guid(server, ada_id)
    app: Any = server.app
    count = server.portal.call(  # type: ignore[attr-defined]
        app.state.database.fetch_value,
        "SELECT COUNT(*) FROM conversations WHERE user_guid = ?",
        (guid,),
    )
    assert count == 2
    (event,) = _events(server)
    assert event.outcome == "success"
    assert event.actor == admin_id
    assert event.resource_id == ada_id
    details = event.details
    assert details["rows"] == 36 and details["dropped"] == 8
    assert details["format_version"] == bf.FORMAT_VERSION
    assert "content" not in json.dumps(details)


@pytest.mark.postgres
def test_imported_through_the_page_matches_the_source_row_for_row(
    server: TestClient, bundle: tuple[bytes, Path]
) -> None:
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    _offer_remote_search(admin)
    assert _upload(admin, bundle[0], ada_id).status_code == 200
    guid = _guid(server, ada_id)
    app: Any = server.app
    with open_bundle(io.BytesIO(bundle[0]), Limits()) as opened:
        bundle_id = opened.bundle_id

    async def check() -> dict[str, int]:
        source = await Database.open(DatabaseSettings(Dialect.SQLITE, sqlite_path=bundle[1]))
        try:
            return await compare(source, PERSONAL_USER, app.state.database, guid, bundle_id)
        finally:
            await source.close()

    compared: dict[str, int] = server.portal.call(check)  # type: ignore[attr-defined]
    assert sum(compared.values()) == 36


@pytest.mark.postgres
@pytest.mark.parametrize("role", [USER, POWER_USER])
def test_only_an_administrator_may_import(
    server: TestClient, bundle: tuple[bytes, Path], role: str
) -> None:
    browser, user_id = _signed_in(server, "mallory@example.com", role)
    assert browser.get("/admin/bundles").status_code == 403
    resp = browser.post(
        "/admin/bundles",
        data={"user_id": user_id},
        files={"bundle": ("b.zip", bundle[0], "application/zip")},
        headers={"Origin": PUBLIC},
    )
    assert resp.status_code == 403
    assert _events(server) == []
    count = server.portal.call(  # type: ignore[attr-defined]
        server.app.state.database.fetch_value, "SELECT COUNT(*) FROM conversations", ()  # type: ignore[attr-defined]
    )
    assert count == 0


@pytest.mark.postgres
def test_signed_out_is_refused(server: TestClient) -> None:
    browser = another_client(server)
    assert browser.get("/admin/bundles", headers=FETCH).status_code == 401


@pytest.mark.postgres
def test_reimporting_is_refused_on_screen_and_audited(
    server: TestClient, bundle: tuple[bytes, Path]
) -> None:
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    assert _upload(admin, bundle[0], ada_id).status_code == 200
    again = _upload(admin, bundle[0], ada_id)
    assert again.status_code == 409
    assert "already imported" in again.text
    outcomes = sorted(e.outcome for e in _events(server))
    assert outcomes == ["failure", "success"]


@pytest.mark.postgres
def test_a_bundle_over_the_size_limit_is_refused(
    server: TestClient, bundle: tuple[bytes, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundles_endpoint, "LIMITS", Limits(max_bundle_bytes=1024))
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    resp = _upload(admin, bundle[0], ada_id)
    assert resp.status_code == 413
    assert "larger than" in resp.text


@pytest.mark.postgres
def test_another_format_version_is_refused(server: TestClient, bundle: tuple[bytes, Path]) -> None:
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(bundle[0])) as zf:
        for name in zf.namelist():
            members[name] = zf.read(name)
    manifest = json.loads(members[bf.MANIFEST])
    manifest["format_version"] = 2
    members[bf.MANIFEST] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    resp = _upload(admin, out.getvalue(), ada_id)
    assert resp.status_code == 400
    assert "version" in resp.text


@pytest.mark.postgres
def test_an_unknown_target_user_is_refused(server: TestClient, bundle: tuple[bytes, Path]) -> None:
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    resp = _upload(admin, bundle[0], str(uuid.uuid4()))
    assert resp.status_code == 400
    assert "Choose a user" in resp.text
    resp = _upload(admin, bundle[0], "not-a-uuid")
    assert resp.status_code == 400


@pytest.mark.postgres
def test_over_the_quota_is_refused(server: TestClient, bundle: tuple[bytes, Path]) -> None:
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    app: Any = server.app
    # Ada's area already holds her whole quota (1 MB in the fixture).
    server.portal.call(  # type: ignore[attr-defined]
        app.state.database.execute,
        "INSERT INTO user_file_usage (user_guid, bytes_used) VALUES (?, ?)",
        (_guid(server, ada_id), 1 << 20),
    )
    resp = _upload(admin, bundle[0], ada_id)
    assert resp.status_code == 507
    assert "quota" in resp.text


@pytest.mark.postgres
def test_server_mode_has_no_export(server: TestClient) -> None:
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    assert admin.get("/bundles/export").status_code == 404


@pytest.mark.postgres
def test_the_menu_offers_the_import_to_administrators_only(server: TestClient) -> None:
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    user, _ = _signed_in(server, "ada@example.com", USER)
    assert 'href="/admin/bundles"' in admin.get("/account").text
    assert 'href="/admin/bundles"' not in user.get("/account").text


# -- The upload limit, before the bundle is opened --------------------------------------


class _Body:
    """A request whose body arrives in chunks, with or without a declared length."""

    def __init__(self, chunks: list[bytes], declared: int | None) -> None:
        self._chunks = chunks
        self.headers = {} if declared is None else {"content-length": str(declared)}

    async def stream(self) -> Any:
        for chunk in self._chunks:
            yield chunk


async def test_the_upload_is_cut_off_while_it_streams() -> None:
    body = _Body([b"x" * 600, b"x" * 600], None)
    received: list[bytes] = []
    with pytest.raises(bundles_endpoint._TooLarge):
        async for chunk in bundles_endpoint._capped(body, 1000):  # type: ignore[arg-type]
            received.append(chunk)
    assert received == [b"x" * 600]
    within = [c async for c in bundles_endpoint._capped(_Body([b"x" * 1000], None), 1000)]  # type: ignore[arg-type]
    assert within == [b"x" * 1000]


@pytest.mark.postgres
def test_a_declared_length_over_the_limit_is_refused_unread(
    server: TestClient, bundle: tuple[bytes, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundles_endpoint, "LIMITS", Limits(max_bundle_bytes=1024))
    monkeypatch.setattr(bundles_endpoint, "_MULTIPART_OVERHEAD", 0)
    monkeypatch.setattr(
        bundles_endpoint, "open_bundle", lambda *a: pytest.fail("the bundle must not be opened")
    )
    monkeypatch.setattr(
        bundles_endpoint.MultiPartParser, "parse", lambda self: pytest.fail("must not be read")
    )
    admin, _ = _signed_in(server, "root@example.com", ADMINISTRATOR)
    _, ada_id = _signed_in(server, "ada@example.com", USER)
    resp = _upload(admin, bundle[0], ada_id)
    assert resp.status_code == 413
    assert "larger than" in resp.text
