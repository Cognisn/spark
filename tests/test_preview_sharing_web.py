"""Server mode: a document shown in a shared conversation, for every member (CS39).

The real app on a PostgreSQL clone with warden's chain, over conduit's stub provider
(the fixtures of test_conversation_sharing_web.py). Ada owns a conversation shared
with Cora (contributor) and Vic (read-only); Sam is a stranger.

Cora attaches a file and asks; the stub model calls ``show_document`` with its id. The
tool runs as Cora (her permissions, her conversation), and the result carries the card
the chat page draws. Then every member opens the card's file through the conversation's
access decision; the stranger gets 404; a member removed from the conversation gets 404
at once; and each opening is audited.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from conduit import ModelRequest
from conduit.providers.stub import StubToolCall, StubTurn
from fastapi.testclient import TestClient

from spark.identity import audit as spark_audit
from spark.preview.service import parse_card
from tests.preview_helpers import pdf_bytes
from tests.test_conversation_sharing_web import (  # noqa: F401
    Person,
    events,
    install_runtime,
    new_conversation,
    people,
    person,
    share,
)
from tests.test_user_scoping_web import client, host, server_db  # noqa: F401

pytestmark = pytest.mark.postgres


class ShowScript:
    """The stub's answers: show the file named in ``file_id``, then say so."""

    def __init__(self) -> None:
        self.file_id: int | None = None
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        with self.lock:
            self.calls += 1
            first = self.calls % 2 == 1
        if first:
            return StubTurn(
                tool_calls=(
                    StubToolCall(
                        name="show_document",
                        input={"file_id": self.file_id, "title": "Briefing"},
                        id=f"call-{self.calls}",
                    ),
                )
            )
        return StubTurn(text="Here it is.")


@pytest.fixture
def script(client: TestClient) -> ShowScript:  # noqa: F811
    shown = ShowScript()
    install_runtime(client.app, shown, ("user", "power_user", "auditor", "administrator"))  # type: ignore[arg-type]
    return shown


@pytest.fixture
def shared(people: dict[str, Person]) -> int:  # noqa: F811
    cid = new_conversation(people["ada"])
    assert share(people["ada"], cid, people["cora"], "contributor").json()["status"] == "added"
    assert share(people["ada"], cid, people["vic"], "viewer").json()["status"] == "added"
    return cid


def _cards(person_: Person, cid: int) -> list[Any]:
    history = person_.browser.get(f"/chat/{cid}/api/history").json()
    found = []
    for message in history:
        content = message.get("content") or ""
        content = content.removeprefix("[TOOL_RESULTS]")
        if not content.startswith("["):
            continue
        try:
            blocks = json.loads(content)
        except ValueError:
            continue
        for block in blocks if isinstance(blocks, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                card = parse_card(str(block.get("content", "")))
                if card is not None:
                    found.append(card)
    return found


def test_every_member_opens_the_document_and_nobody_else(
    client: TestClient,  # noqa: F811
    people: dict[str, Person],  # noqa: F811
    script: ShowScript,
    shared: int,
) -> None:
    cora = people["cora"]
    upload = cora.browser.post(
        "/files/api",
        files={"file": ("briefing.pdf", pdf_bytes(1), "application/pdf")},
        data={"conversation_id": str(shared)},
    )
    assert upload.status_code == 201, upload.text
    script.file_id = int(upload.json()["file"]["id"])

    sent = cora.browser.post(f"/chat/{shared}/api/send", json={"message": "Show me the briefing"})
    assert sent.status_code == 200, sent.text

    # The card is in the conversation, for every member to see.
    cards = _cards(people["vic"], shared)
    assert len(cards) == 1
    card = cards[0]
    assert (card.file_id, card.conversation_id, card.title, card.kind) == (
        script.file_id,
        shared,
        "Briefing",
        "pdf",
    )
    url = f"/preview/api/{shared}/files/{card.file_id}"
    for who in ("ada", "cora", "vic"):
        got = people[who].browser.get(url)
        assert got.status_code == 200, who
        assert got.content.startswith(b"%PDF-")
    assert people["sam"].browser.get(url).status_code == 404

    # A member removed from the conversation is refused at once.
    removed = people["ada"].browser.delete(
        f"/conversations/api/{shared}/members/{people['vic'].guid}"
    )
    assert removed.status_code == 200, removed.text
    assert people["vic"].browser.get(url).status_code == 404
    assert people["vic"].browser.get(f"{url}?download=1").status_code == 404

    # Each opening is audited as a download, with how it was opened.
    rows = events(client, spark_audit.FILE_DOWNLOADED)
    previews = [r for r in rows if (r.details or {}).get("disposition") == "preview"]
    assert len(previews) == 3
    assert {r.resource_id for r in previews} == {str(card.file_id)}


def test_a_viewer_cannot_make_the_ai_show_anything(
    client: TestClient,  # noqa: F811
    people: dict[str, Person],  # noqa: F811
    script: ShowScript,
    shared: int,
) -> None:
    sent = people["vic"].browser.post(f"/chat/{shared}/api/send", json={"message": "show it"})
    assert sent.status_code == 403
    assert script.calls == 0
