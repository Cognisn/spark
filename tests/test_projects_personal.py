"""Personal mode (PC15): projects, sub-projects, instructions, resources in the personal
file area, moves and deletion work; sharing, members and transfer answer 404."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversations
from spark.mode import Mode

MODEL = "stub/model-a"
ME = "u1"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def personal(sqlite_settings: Any, tmp_path: Path) -> Iterator[TestClient]:
    from spark.web.server import create_app
    from tests.db_helpers import app_client
    from tests.test_voice_endpoints import _mock_settings_get
    from tests.web_helpers import sign_in as personal_sign_in

    files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    try:
        with app_client(app, sqlite_settings) as c:
            app.state.user_guid = ME
            personal_sign_in(c)
            yield c
    finally:
        files_pkg.reset()


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def test_projects_work_without_sharing(personal: TestClient, tmp_path: Path) -> None:
    c = personal
    top = c.post("/projects/api", json={"name": "Home", "instructions": "Be kind."})
    assert top.status_code == 201, top.text
    pid = int(top.json()["project"]["id"])
    sub = c.post("/projects/api", json={"name": "Garden", "parent_id": pid})
    assert sub.status_code == 201
    sub_id = int(sub.json()["project"]["id"])

    made = c.post(
        "/projects/api/{}/resources".format(sub_id),
        files={"file": ("seeds.txt", b"tomatoes", "text/plain")},
    )
    assert made.status_code == 201, made.text
    fid = made.json()["resource"]["file_id"]
    # The personal area under the data directory holds it.
    assert any(p.name == "seeds.txt" for p in (tmp_path / "files").rglob("*"))
    assert c.get(f"/projects/api/{sub_id}/resources/{fid}").content == b"tomatoes"

    cid = _call(c, conversations.create_conversation, "Plants", MODEL, ME)
    moved = c.post(f"/projects/api/conversations/{cid}/move", json={"project_id": sub_id})
    assert moved.status_code == 200 and moved.json()["moved"], moved.text
    context = c.get(f"/projects/api/conversations/{cid}/context").json()
    assert [p["name"] for p in context["path"]] == ["Home", "Garden"]

    tree = c.get("/projects/api").json()
    assert [n["name"] for n in tree["owned"]] == ["Home"]
    assert [x["id"] for x in tree["owned"][0]["children"][0]["conversations"]] == [cid]

    # Sharing, members and transfer are server mode only.
    assert c.get(f"/projects/api/{pid}/members").status_code == 404
    assert c.get(f"/projects/api/{pid}/people").status_code == 404
    assert (
        c.post(
            f"/projects/api/{pid}/members",
            json={"user_guid": "u2", "role": "viewer", "scope": "project_only"},
        ).status_code
        == 404
    )
    assert c.post(f"/projects/api/{pid}/transfer", json={"user_guid": "u2"}).status_code == 404
    assert c.get(f"/projects/api/{pid}").json()["members"] is None

    done = c.post(f"/projects/api/{pid}/delete", json={"mode": "keep"})
    assert done.status_code == 200 and done.json()["conversations_kept"] == 1
    row = _call(c, conversations.get_conversation, cid, ME)
    assert row["project_id"] is None
    assert not any(p.name == "seeds.txt" for p in (tmp_path / "files").rglob("*"))
