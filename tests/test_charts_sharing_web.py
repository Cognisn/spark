"""Server mode: a chart made in a shared conversation, for every member (PC19, PC20).

The real app on a PostgreSQL clone with warden's chain, over conduit's stub provider
(the fixtures of test_conversation_sharing_web.py). Ada owns a conversation shared
with Cora (contributor) and Vic (read-only); Sam is a stranger with a file of his own.

Cora attaches a CSV and asks; the stub model calls ``create_chart`` with the file's id.
The tool runs as Cora, the chart is kept in her area as a file of the conversation, and
the result carries the card the chat page draws. Every member then opens the chart's
option and its data through the conversation's access decision; Sam gets 404; a
member removed from the conversation gets 404 at once; the data download is audited.
A chart naming Sam's file is refused as not found.
"""

from __future__ import annotations

import json
import threading

import pytest
from conduit import ModelRequest
from conduit.providers.stub import StubToolCall, StubTurn
from fastapi.testclient import TestClient

from spark.identity import audit as spark_audit
from spark.preview.service import parse_card
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


class ChartScript:
    """The stub's answers: chart the file named in ``file_id``, then say so."""

    def __init__(self) -> None:
        self.file_id: int | None = None
        self.calls = 0
        self.results: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        with self.lock:
            self.calls += 1
            first = self.calls % 2 == 1
        if first:
            definition = {
                "type": "bar",
                "data": {"file_id": self.file_id},
                "encoding": {"x": "Quarter", "y": "Sales"},
            }
            return StubTurn(
                tool_calls=(
                    StubToolCall(
                        name="create_chart",
                        input={"title": "Sales by quarter", "definition": definition},
                        id=f"call-{self.calls}",
                    ),
                )
            )
        return StubTurn(text="Here it is.")


@pytest.fixture
def script(client: TestClient) -> ChartScript:  # noqa: F811
    charted = ChartScript()
    install_runtime(client.app, charted, ("user", "power_user", "auditor", "administrator"))  # type: ignore[arg-type]
    return charted


@pytest.fixture
def shared(people: dict[str, Person]) -> int:  # noqa: F811
    cid = new_conversation(people["ada"])
    assert share(people["ada"], cid, people["cora"], "contributor").json()["status"] == "added"
    assert share(people["ada"], cid, people["vic"], "viewer").json()["status"] == "added"
    return cid


def _results(person_: Person, cid: int) -> list[str]:
    history = person_.browser.get(f"/chat/{cid}/api/history").json()
    found = []
    for message in history:
        content = (message.get("content") or "").removeprefix("[TOOL_RESULTS]")
        if not content.startswith("["):
            continue
        try:
            blocks = json.loads(content)
        except ValueError:
            continue
        for block in blocks if isinstance(blocks, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append(str(block.get("content", "")))
    return found


def _upload(who: Person, name: str, data: bytes, cid: int | None) -> int:
    form = {"conversation_id": str(cid)} if cid is not None else {}
    got = who.browser.post("/files/api", files={"file": (name, data, "text/csv")}, data=form)
    assert got.status_code == 201, got.text
    return int(got.json()["file"]["id"])


def test_every_member_opens_the_chart_and_nobody_else(
    client: TestClient,  # noqa: F811
    people: dict[str, Person],  # noqa: F811
    script: ChartScript,
    shared: int,
) -> None:
    cora = people["cora"]
    script.file_id = _upload(cora, "sales.csv", b"Quarter,Sales\nQ1,10\nQ2,14\n", shared)
    sent = cora.browser.post(f"/chat/{shared}/api/send", json={"message": "Chart the sales"})
    assert sent.status_code == 200, sent.text

    cards = [c for c in (parse_card(r) for r in _results(people["vic"], shared)) if c]
    assert len(cards) == 1
    card = cards[0]
    assert (card.kind, card.conversation_id, card.title) == ("chart", shared, "Sales by quarter")
    url = f"/charts/api/{card.file_id}"
    for who in ("ada", "cora", "vic"):
        got = people[who].browser.get(f"{url}?theme=dark")
        assert got.status_code == 200, who
        assert got.json()["option"]["series"][0]["data"] == [10, 14]
        assert people[who].browser.get(f"{url}/data.csv").status_code == 200, who
    assert people["sam"].browser.get(url).status_code == 404
    assert people["sam"].browser.get(f"{url}/data.csv").status_code == 404

    removed = people["ada"].browser.delete(
        f"/conversations/api/{shared}/members/{people['vic'].guid}"
    )
    assert removed.status_code == 200, removed.text
    assert people["vic"].browser.get(url).status_code == 404

    rows = events(client, spark_audit.FILE_DOWNLOADED)
    downloads = [r for r in rows if (r.details or {}).get("disposition") == "chart_data"]
    assert {r.resource_id for r in downloads} == {str(card.file_id)} and len(downloads) == 3


def test_another_users_file_is_not_found(
    client: TestClient,  # noqa: F811
    people: dict[str, Person],  # noqa: F811
    script: ChartScript,
    shared: int,
) -> None:
    script.file_id = _upload(people["sam"], "his.csv", b"Quarter,Sales\nQ1,1\n", None)
    sent = people["cora"].browser.post(f"/chat/{shared}/api/send", json={"message": "Chart it"})
    assert sent.status_code == 200, sent.text
    results = _results(people["ada"], shared)
    assert any(
        f"File {script.file_id} is not one of this conversation's files or yours." in r
        for r in results
    ), results
    assert not [c for c in (parse_card(r) for r in results) if c]


def test_a_viewer_cannot_make_the_ai_chart_anything(
    client: TestClient,  # noqa: F811
    people: dict[str, Person],  # noqa: F811
    script: ChartScript,
    shared: int,
) -> None:
    sent = people["vic"].browser.post(f"/chat/{shared}/api/send", json={"message": "chart"})
    assert sent.status_code == 403
    assert script.calls == 0
