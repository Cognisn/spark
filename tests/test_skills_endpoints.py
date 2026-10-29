"""Skills page API tests: list, toggle, view, import, download, delete."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.skills.manager import SkillsManager, set_skills_manager
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_skills_manager import make_skill
from tests.web_helpers import sign_in


@pytest.fixture()
def skills_env(tmp_path):
    make_skill(tmp_path / "user", "pdf-filler")
    m = SkillsManager(tmp_path / "user")
    set_skills_manager(m)
    yield m
    set_skills_manager(None)


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture()
def client(sqlite_settings, skills_env) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)

    with app_client(app, sqlite_settings) as client:
        db = app.state.sync_db
        mgr = MagicMock()
        mgr._db = db

        def _create(name, model_id, user_guid, **kwargs):
            from spark.database import conversations as convdb

            return db.call(convdb.create_conversation, name, model_id, user_guid)

        mgr.create_conversation.side_effect = _create
        app.state.conversation_manager = mgr
        app.state.skills_manager = skills_env
        app.state.user_guid = "test-user"
        yield client


def _auth(client: TestClient) -> None:
    sign_in(client)


class TestListAndToggle:
    def test_list_includes_enabled_state(self, client) -> None:
        _auth(client)
        data = client.get("/skills/api/list").json()["skills"]
        entry = next(s for s in data if s["name"] == "pdf-filler")
        assert entry["enabled"] is True and entry["source"] == "user"

    def test_toggle_persists(self, client) -> None:
        _auth(client)
        client.post("/skills/api/toggle", json={"name": "pdf-filler", "enabled": False})
        data = client.get("/skills/api/list").json()["skills"]
        assert next(s for s in data if s["name"] == "pdf-filler")["enabled"] is False


class TestView:
    def test_view_returns_body_and_resources(self, client) -> None:
        _auth(client)
        data = client.get("/skills/api/view?name=pdf-filler").json()
        assert "Do it well." in data["body"]
        assert client.get("/skills/api/view?name=ghost").status_code == 404


class TestImportDownloadDelete:
    def _zip_of(self, name: str, skill_md: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{name}/SKILL.md", skill_md)
        return buf.getvalue()

    def test_import_valid_zip(self, client) -> None:
        _auth(client)
        payload = self._zip_of(
            "imported-skill", "---\nname: imported-skill\ndescription: d\n---\nbody\n"
        )
        r = client.post("/skills/api/import", files={"file": ("s.zip", payload, "application/zip")})
        assert r.status_code == 200
        names = [s["name"] for s in client.get("/skills/api/list").json()["skills"]]
        assert "imported-skill" in names

    def test_import_collision_rejected(self, client) -> None:
        _auth(client)
        payload = self._zip_of("pdf-filler", "---\nname: pdf-filler\ndescription: d\n---\nbody\n")
        r = client.post("/skills/api/import", files={"file": ("s.zip", payload, "application/zip")})
        assert r.status_code == 400 and "exists" in r.json()["error"]

    def test_import_invalid_zip_rejected(self, client) -> None:
        _auth(client)
        payload = self._zip_of("bad-skill", "no frontmatter at all")
        r = client.post("/skills/api/import", files={"file": ("s.zip", payload, "application/zip")})
        assert r.status_code == 400

    def test_download_and_delete(self, client) -> None:
        _auth(client)
        r = client.get("/skills/api/download?name=pdf-filler")
        assert r.status_code == 200
        assert zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert client.post("/skills/api/delete", json={"name": "pdf-filler"}).status_code == 200
        names = [s["name"] for s in client.get("/skills/api/list").json()["skills"]]
        assert "pdf-filler" not in names

    def test_page_renders(self, client) -> None:
        _auth(client)
        assert "skills" in client.get("/skills").text.lower()


class TestConversationToggles:
    def test_tools_api_lists_skills(self, client) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json={"name": "n", "model_id": "m"}).json()[
            "id"
        ]
        data = client.get(f"/chat/{cid}/api/tools").json()
        assert any(s["name"] == "pdf-filler" and s["enabled"] for s in data["skills"])

    def test_toggle_skill_for_conversation(self, client) -> None:
        _auth(client)
        cid = client.post("/conversations/api/create", json={"name": "n", "model_id": "m"}).json()[
            "id"
        ]
        client.post(
            f"/chat/{cid}/api/tools",
            json={"type": "skill", "name": "pdf-filler", "enabled": False},
        )
        data = client.get(f"/chat/{cid}/api/tools").json()
        assert not next(s for s in data["skills"] if s["name"] == "pdf-filler")["enabled"]
