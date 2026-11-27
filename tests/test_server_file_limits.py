"""Server mode: the administrator's file limits reach every path that enforces them.

``files.quota_mb`` and ``files.max_upload_mb`` are instance settings. Each test sets one
through the Settings API as an administrator (no restart), watches a path refuse with
its existing status, lifts the setting and watches the same request pass. The start-up
settings deliberately say something else, so a path reading them would pass the refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark.identity.permissions import ADMINISTRATOR
from tests.test_conversation_sharing_web import FETCH, Person, person
from tests.test_projects_web import (  # noqa: F401  (fixtures)
    client,
    mail,
    people,
    plan,
    script,
    server_db,
    storage,
    upload,
    usage,
)
from tests.test_user_scoping_web import host  # noqa: F401  (fixture)

pytestmark = pytest.mark.postgres

MB = 1024 * 1024
OVER_A_MEGABYTE = 1_200_000


@pytest.fixture
def admin(people: dict[str, Person], client: Any) -> Person:  # noqa: F811
    return person(client, "root@example.com", ADMINISTRATOR)


def set_limits(admin: Person, **values: int) -> None:
    resp = admin.browser.post(
        "/settings/api/instance", json={f"files.{key}": value for key, value in values.items()}
    )
    assert resp.status_code == 200, resp.text


def plain_upload(who: Person, size: int, **form: Any) -> Any:
    return who.browser.post(
        "/files/api", files={"file": ("data.bin", b"x" * size, "text/plain")}, data=form
    )


class TestUploadSize:
    def test_the_administrators_maximum_applies_at_once(
        self, people: dict[str, Person], admin: Person
    ) -> None:
        cora = people["cora"]
        assert plain_upload(cora, OVER_A_MEGABYTE).status_code == 201
        set_limits(admin, max_upload_mb=1)
        refused = plain_upload(cora, OVER_A_MEGABYTE)
        assert refused.status_code == 413
        assert "maximum upload size" in refused.json()["error"]
        set_limits(admin, max_upload_mb=50)
        assert plain_upload(cora, OVER_A_MEGABYTE).status_code == 201

    def test_the_limit_is_shown_on_the_listing(
        self, people: dict[str, Person], admin: Person
    ) -> None:
        set_limits(admin, max_upload_mb=3, quota_mb=7)
        body = people["cora"].browser.get("/files/api", headers=FETCH).json()
        assert (body["max_upload_bytes"], body["quota_bytes"]) == (3 * MB, 7 * MB)


class TestQuota:
    def test_the_administrators_quota_applies_to_uploads(
        self, people: dict[str, Person], admin: Person
    ) -> None:
        cora = people["cora"]
        set_limits(admin, quota_mb=1)
        assert plain_upload(cora, 600_000).status_code == 201
        refused = plain_upload(cora, 600_000)
        assert refused.status_code == 507 and "quota" in refused.json()["error"].lower()
        set_limits(admin, quota_mb=0)
        assert plain_upload(cora, 600_000).status_code == 201

    def test_a_chat_attachment_is_charged_to_the_quota(
        self, people: dict[str, Person], admin: Person
    ) -> None:
        cora = people["cora"]
        made = cora.browser.post("/conversations/api/create", json={"name": "A", "model_id": "m"})
        cid = str(made.json()["id"]) if made.status_code == 200 else ""
        set_limits(admin, quota_mb=1)
        refused = plain_upload(cora, OVER_A_MEGABYTE, conversation_id=cid)
        assert refused.status_code == 507
        set_limits(admin, quota_mb=2)
        assert plain_upload(cora, OVER_A_MEGABYTE, conversation_id=cid).status_code == 201

    def test_a_project_resource_is_charged_to_the_owners_quota(
        self, people: dict[str, Person], plan: dict[str, int], admin: Person
    ) -> None:
        set_limits(admin, quota_mb=1)
        refused = upload(people["cora"], plan["plan"], "big.bin", b"x" * OVER_A_MEGABYTE)
        assert refused.status_code == 507 and "owner's storage" in refused.json()["error"]
        set_limits(admin, quota_mb=2)
        assert (
            upload(people["cora"], plan["plan"], "big.bin", b"x" * OVER_A_MEGABYTE).status_code
            == 201
        )

    def test_a_transfer_is_refused_when_the_new_owner_lacks_the_quota(
        self, people: dict[str, Person], plan: dict[str, int], admin: Person, mail: Any
    ) -> None:
        ada, cora = people["ada"], people["cora"]
        assert upload(ada, plan["plan"], "data.bin", b"d" * 400_000).status_code == 201
        assert plain_upload(cora, 700_000).status_code == 201
        set_limits(admin, quota_mb=1)
        url = f"/projects/api/{plan['plan']}/transfer"
        refused = ada.browser.post(url, json={"user_guid": cora.guid})
        assert refused.status_code == 507
        assert usage(ada) == 400_000
        set_limits(admin, quota_mb=0)
        assert ada.browser.post(url, json={"user_guid": cora.guid}).status_code == 200
