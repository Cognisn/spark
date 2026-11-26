"""Helpers the projects, charts, panel and skills live checks share (wave 32).

Model turns are scripted for the stack's model stub (``deploy/live/model_stub.py``):
``call64`` asks the stub to call a tool with arguments that hold lists, which the
plain ``call`` directive cannot carry. What SPARK sent the model is read back from
the stub's record (``/_stub/requests``): the system prompt, the tools offered and
each tool result.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import time
from typing import Any

import httpx

from tests.live.client import Browser, Event, named
from tests.live.conftest import Person, Stack

PROJECT_CARD = re.compile(r"\[spark-project-card\] (\{[^\n]*\})")
PREVIEW_CARD = re.compile(r"\[spark-preview\] (\{[^\n]*\})")


def call64(tool: str, arguments: dict[str, Any]) -> str:
    """The stub directive that calls ``tool`` with ``arguments`` (lists allowed)."""
    raw = base64.b64encode(json.dumps(arguments).encode()).decode()
    return f"[[call64 {tool} {raw}]]"


def chats_since(stack: Stack, since: float) -> list[dict[str, Any]]:
    return [r for r in stack.stub_requests() if r["api"] == "ollama.chat" and r["at"] >= since]


def turn(person: Person, conversation_id: int, message: str, **kwargs: Any) -> list[Event]:
    """One chat turn; asserts the stream completed."""
    events = person.browser.chat(conversation_id, message, **kwargs)
    assert named(events, "complete"), [(e.name, e.data) for e in events][-5:]
    return events


def tool_records(
    stack: Stack, person: Person, conversation_id: int, text: str, tool: str, arguments: Any
) -> tuple[list[Event], dict[str, Any], dict[str, Any]]:
    """A turn in which the model calls ``tool`` (answering its permission prompt once):
    the events, the first request the model was sent, and the request that carried the
    tool's result back to the model."""
    marker = f"turn-{time.time_ns()}"
    since = time.time()
    events = turn(
        person, conversation_id, f"{text} {marker} {call64(tool, arguments)}", approve="once"
    )
    sent = [r for r in chats_since(stack, since) if marker in r["user"]]
    assert sent, "the turn never reached the model"
    follow = [r for r in sent if r["last_role"] == "tool"]
    assert follow, f"SPARK did not answer {tool}: {[r['last'][:200] for r in sent]}"
    return events, sent[0], follow[0]


def tool_turn(
    stack: Stack, person: Person, conversation_id: int, text: str, tool: str, arguments: Any
) -> tuple[list[Event], dict[str, Any], str]:
    """As :func:`tool_records`, with the tool result SPARK passed back to the model."""
    events, first, follow = tool_records(stack, person, conversation_id, text, tool, arguments)
    return events, first, str(follow["last"])


def preview_card(result: str) -> dict[str, Any]:
    found = PREVIEW_CARD.search(result)
    assert found, result[:2000]
    card: dict[str, Any] = json.loads(found.group(1))
    return card


def project_card(result: str) -> dict[str, Any]:
    found = PROJECT_CARD.search(result)
    assert found, result[:2000]
    card: dict[str, Any] = json.loads(found.group(1))
    return card


def offers_skill(system: str, name: str) -> bool:
    """Whether a system prompt's Available Skills block offers the skill ``name``
    (``- name: ...`` or, from conduit, ``- name (version V): ...``)."""
    return re.search(rf"^- {re.escape(name)}(?: \(version [^)]*\))?: ", system, re.M) is not None


def ok(resp: httpx.Response, *codes: int) -> Any:
    """The JSON body of a response whose status is one of ``codes`` (200 by default)."""
    wanted = codes or (200,)
    assert resp.status_code in wanted, (resp.status_code, resp.text[:2000])
    return resp.json() if resp.content else None


def new_conversation(
    person: Person,
    model_id: str,
    name: str,
    *,
    project_id: int | None = None,
    instructions: str | None = None,
) -> int:
    body: dict[str, Any] = {"name": name, "model_id": model_id}
    if project_id is not None:
        body["project_id"] = project_id
    if instructions is not None:
        body["instructions"] = instructions
    found = ok(person.browser.post("/conversations/api/create", json=body), 200, 201)
    return int(found.get("conversation_id") or found.get("id") or found["conversation"]["id"])


def upload(
    browser: Browser,
    name: str,
    body: bytes,
    mime: str = "text/plain",
    conversation_id: int | None = None,
) -> int:
    """A file into the person's area (attached to ``conversation_id`` when given)."""
    data = {"conversation_id": str(conversation_id)} if conversation_id else {}
    found = ok(browser.post("/files/api", files={"file": (name, body, mime)}, data=data), 201)
    return int(found["file"]["id"])


def create_project(
    browser: Browser,
    name: str,
    *,
    parent_id: int | None = None,
    instructions: str = "",
    description: str = "",
) -> int:
    body = {
        "name": name,
        "parent_id": parent_id,
        "instructions": instructions,
        "description": description,
    }
    return int(ok(browser.post("/projects/api", json=body), 201)["project"]["id"])


def add_resource(
    browser: Browser, project_id: int, name: str, body: bytes, mime: str = "text/plain"
) -> int:
    resp = browser.post(f"/projects/api/{project_id}/resources", files={"file": (name, body, mime)})
    return int(ok(resp, 201)["resource"]["file_id"])


def csv_bytes(columns: list[str], rows: list[list[Any]]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def xlsx_bytes(sheet: str, columns: list[str], rows: list[list[Any]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    page = book.active
    assert page is not None
    page.title = sheet
    page.append(columns)
    for row in rows:
        page.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def can_read(person: Person, conversation_id: int) -> bool:
    """Whether ``person`` may read the conversation (its members list answers them)."""
    resp = person.browser.get(f"/conversations/api/{conversation_id}/members")
    assert resp.status_code in (200, 404), (resp.status_code, resp.text[:500])
    return resp.status_code == 200


def role_in(person: Person, conversation_id: int) -> str | None:
    resp = person.browser.get(f"/conversations/api/{conversation_id}/members")
    if resp.status_code == 404:
        return None
    return str(ok(resp)["role"])


def instance(admin: Person, values: dict[str, Any]) -> None:
    ok(admin.browser.post("/settings/api/instance", json=values))
