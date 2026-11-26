"""A rename reaches warden's audit trail in server mode, through the real app (PC5).

Ada renames a file of her conversation: one ``spark.artefact.renamed`` row whose actor is
the signed-in warden user, naming the file and the conversation, with the title nowhere in
its details. A person who is not a member, and a title that is refused, leave no row.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations
from tests.test_spark_audit_web import (  # noqa: F401  (fixtures)
    _call,
    _rows,
    _signed_in,
    client,
    server_db,
)

pytestmark = pytest.mark.postgres

ACTION = "spark.artefact.renamed"


def _attach(browser: TestClient, cid: int) -> int:
    upload = browser.post(
        "/files/api",
        data={"conversation_id": str(cid)},
        files={"file": ("plan.txt", b"The plan", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    return int(upload.json()["file"]["id"])


def test_a_rename_is_audited_without_the_title(client: TestClient) -> None:  # noqa: F811
    ada, guid, warden_id = _signed_in(client, "ada@example.com")
    cid = _call(client, conversations.create_conversation, "Ada's notes", "model-a", guid)
    fid = _attach(ada, cid)
    got = ada.patch(f"/artefacts/api/{fid}", json={"title": "Confidential quarterly plan"})
    assert got.status_code == 200, got.text
    [row] = _rows(client, ACTION)
    assert (row.actor, row.resource_type, row.resource_id) == (warden_id, "file", str(fid))
    assert row.outcome == "success"
    assert row.details["conversation_id"] == cid
    assert "Confidential" not in repr(row.details)
    listing = ada.get(f"/artefacts/api/{cid}").json()
    assert [i["title"] for i in listing["provided"]] == ["Confidential quarterly plan"]


def test_a_stranger_and_a_refused_title_leave_no_row(client: TestClient) -> None:  # noqa: F811
    ada, guid, _ = _signed_in(client, "ada@example.com")
    grace, _, _ = _signed_in(client, "grace@example.com")
    cid = _call(client, conversations.create_conversation, "Ada's notes", "model-a", guid)
    fid = _attach(ada, cid)
    assert grace.patch(f"/artefacts/api/{fid}", json={"title": "Mine"}).status_code == 404
    assert grace.get(f"/artefacts/api/{cid}").status_code == 404
    assert ada.patch(f"/artefacts/api/{fid}", json={"title": ""}).status_code == 400
    assert _rows(client, ACTION) == []
