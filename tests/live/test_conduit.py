"""The 0.3.0 live checks for conduit, connections and credits, against the stack in ``deploy/live``.

Each test names the kick-off scope item it proves (``scope N``) of the workstream
``test/0.3.0-live-e2e-conduit``:

2. connections: two of one service and one of another added through the
   connections API (the modal's), discovery, a missing model declared as an
   overlay, a model outside a connection's residency policy shown as refused;
3. assignment: a connection hidden from a user without its role, a user's own
   allowance over their role's, the higher of two roles' allowances;
4. credits: remaining credits for the picker, the 80% warning, a refused new
   request at 100% while a turn in flight finishes, the usage CSV per connection;
5. MCP over HTTP through conduit with its audit event (A12), a skill loaded with
   ``use_skill``, a forced compaction that keeps the conversation usable;
6. personal mode: a 0.2-style ``config.yaml`` with two providers converted into
   two named connections on the first start, and only once.

Model turns go to the scripted model server (``deploy/live/model_stub.py``)
through conduit's own Ollama adapter; the Anthropic connections only discover,
over https behind the stack's CA. Users, keys and data are synthetic.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.live.client import Browser, Event, final_response, final_text, named
from tests.live.conftest import (
    STUB_CONNECTION,
    STUB_MODEL,
    TINY_MODEL,
    Person,
    Stack,
    assign,
    connection,
    declare,
    invite_and_accept,
    offered,
    wait_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]

#: SPARK's default input and output weights and base rate (spark.usage.credits).
INPUT_WEIGHT, OUTPUT_WEIGHT = 1, 5  # the stub reports no cache tokens
BASE_RATE = 1000


def _stub_url(stack: Stack) -> str:
    return str(stack.state.get("stub_internal", "http://model-stub:11434"))


def _stub_tls_url(stack: Stack) -> str:
    return str(stack.state.get("stub_tls_internal", "https://model-stub:11443"))


def _chats_since(stack: Stack, since: float) -> list[dict[str, Any]]:
    return [r for r in stack.stub_requests() if r["api"] == "ollama.chat" and r["at"] >= since]


def _usage(person: Person, connection_id: str) -> dict[str, Any]:
    resp = person.browser.get("/api/usage/me", params={"connection": connection_id})
    assert resp.status_code == 200, resp.text
    found: dict[str, Any] = resp.json()
    return found


def _view(admin: Person, connection_id: str, *, discover: bool = False) -> dict[str, Any]:
    if discover:
        resp = admin.browser.post(f"/api/connections/{connection_id}/discover")
    else:
        resp = admin.browser.get(f"/api/connections/{connection_id}/models")
    assert resp.status_code == 200, resp.text
    found: dict[str, Any] = resp.json()
    return found


def _rows(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["model_id"]: m for m in view["models"]}


def _metered(admin: Person, connection_id: str) -> None:
    resp = admin.browser.put(f"/api/connections/{connection_id}/metering", json={"metered": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["metered"] is True


def _offered_ids(person: Person) -> set[str]:
    return {str(m["id"]) for m in offered(person)}


def _wait_offered(person: Person, model: str) -> None:
    wait_for(lambda: model in _offered_ids(person), 60, f"{model} offered to {person.email}")


# -- scope 2: connections, discovery, overlays and residency -----------------------------------


def test_connections_of_two_services_discover_declare_and_refuse(
    stack: Stack, admin: Person, configured: None
) -> None:
    """Scope 2: two Ollama connections and one Anthropic connection added through the
    connections API; discovery through conduit; a discovered model the catalogue lacks
    declared as an overlay and then offered; a residency policy refusing every catalogue
    model of the Anthropic connection, shown as refused and never offered."""
    run = stack.run
    first, second, other = f"ollama-a-{run}", f"ollama-b-{run}", f"anthropic-{run}"
    services = {
        s["key"]: s for s in admin.browser.get("/api/connections/services").json()["services"]
    }
    assert services["ollama"]["kind"] == "ollama" and services["anthropic"]["kind"] == "anthropic"

    connection(admin, first, "ollama", f"Ollama A {run}", {"base_url": _stub_url(stack)})
    connection(
        admin, second, "ollama", f"Ollama B {run}", {"base_url": _stub_url(stack), "num_ctx": 8192}
    )
    # Since 0.3.0rc14 a declaration belongs to its connection (Skuld question 53), so
    # each new Ollama connection declares the stub's chat model itself.
    for cid in (first, second):
        declare(admin, cid, STUB_MODEL)
    created = connection(
        admin,
        other,
        "anthropic",
        f"Anthropic {run}",
        {"base_url": _stub_tls_url(stack)},
        secrets={"api_key": f"sk-ant-live-e2e-{run}-dev-only"},
        residency={"scopes": ["local"]},
    )
    assert created["secrets"] == {"api_key": True}, created
    assert created["residency"] == {"scopes": ["local"], "regions": None}, created
    assert "dev-only" not in json.dumps(created), "the key reached a page"
    try:
        listed = {c["id"]: c for c in admin.browser.get("/api/connections").json()["connections"]}
        assert listed[first]["kind"] == listed[second]["kind"] == "ollama", listed
        assert listed[other]["kind"] == "anthropic", listed[other]
        for cid in (first, second, other):
            assert listed[cid]["status"] == "running", listed[cid]

        # Discovery: the stub lists three models the catalogue does not declare; this
        # connection declared one and the other connections' declarations are not its
        # own, so two are left undeclared here.
        view = _view(admin, first, discover=True)
        assert view["discovery"]["ok"], view["discovery"]
        rows = _rows(view)
        assert rows[STUB_MODEL]["overlay"] and rows[STUB_MODEL]["reachable"] is True, rows
        assert rows["llama3.1:8b"]["reachable"] is False, "a catalogue model the server lacks"
        undeclared = {u["model_id"] for u in view["undeclared"]}
        if "extra-stub" not in rows:  # declared by an earlier run on this stack otherwise
            assert "extra-stub" in undeclared, view["undeclared"]
            assert f"{first}/extra-stub" not in _offered_ids(admin)
            declared = _rows(declare(admin, first, "extra-stub", context_window=32768))
            assert declared["extra-stub"]["overlay"], declared
            assert declared["extra-stub"]["refused"] is None, declared["extra-stub"]
        again = _view(admin, first, discover=True)
        assert "extra-stub" not in {u["model_id"] for u in again["undeclared"]}, again
        assert _rows(again)["extra-stub"]["reachable"] is True, again
        _wait_offered(admin, f"{first}/extra-stub")

        # Two connections of one service keep their own settings: the second asks
        # Ollama for its own context length.
        conv = admin.browser.new_conversation(f"{second}/{STUB_MODEL}", f"Connection B {run}")
        since = time.time()
        assert named(admin.browser.chat(conv, f"connection b {run} [[tokens 2]]"), "complete")
        sent = [r for r in _chats_since(stack, since) if run in r["user"]]
        assert sent and sent[0]["num_ctx"] == 8192, sent

        # The Anthropic connection discovers over https behind the stack's CA, and its
        # local-only policy refuses every catalogue model it would otherwise offer.
        view = _view(admin, other, discover=True)
        assert view["discovery"]["ok"], view["discovery"]
        rows = _rows(view)
        assert rows["claude-haiku-5-5"]["reachable"] is True, rows["claude-haiku-5-5"]
        assert {u["model_id"] for u in view["undeclared"]} == {"claude-stub-live-e2e"}, view
        refused = [m for m in rows.values() if m["refused"]]
        assert refused and len(refused) == len(rows), [m["refused"] for m in rows.values()]
        assert "global" in rows["claude-haiku-5-5"]["refused"].lower(), rows["claude-haiku-5-5"]
        assert not [m for m in offered(admin) if m.get("connection_id") == other]
        assert listed[other]["enabled_models"] == 0, listed[other]
    finally:
        for cid in (first, second, other):
            admin.browser.delete(f"/api/connections/{cid}")


def test_a_model_declared_on_a_running_connection_can_be_used_at_once(
    stack: Stack, admin: Person, configured: None
) -> None:
    """Scope 2 (Skuld question 54): a model declared as an overlay on a connection that
    is already running is offered at once, and a turn on it reaches the model.

    Failed on 0.3.0rc9, when the runtime kept the provider built with the catalogue of
    the moment and conduit refused the call (``model ... is not declared in the
    catalogue for 'ollama'``) until a restart. Since 0.3.0rc17 the connection's own
    overlay entries are part of its provider's fingerprint, so the declaration rebuilds
    that provider; passes on 0.3.0rc20.
    """
    run = stack.run
    cid = f"declare-late-{run}"
    model_id = f"late-{run}"
    connection(admin, cid, "ollama", f"Declared late {run}", {"base_url": _stub_url(stack)})
    try:
        # The connection is running (and its provider built) before the declaration.
        assert _view(admin, cid, discover=True)["discovery"]["ok"]
        declare(admin, cid, model_id)
        model = f"{cid}/{model_id}"
        _wait_offered(admin, model)
        conv = admin.browser.new_conversation(model, f"Declared late {run}")
        since = time.time()
        events = admin.browser.chat(conv, f"declared late {run} [[tokens 2]]")
        assert not named(events, "error"), [e.data for e in named(events, "error")]
        assert final_text(events) == "abcd" * 2, [(e.name, e.data) for e in events][-4:]
        assert [r for r in _chats_since(stack, since) if run in r["user"]], "never reached"
    finally:
        admin.browser.delete(f"/api/connections/{cid}")


# -- scope 3: assignment and allowances --------------------------------------------------------


def test_assignment_hides_a_connection_and_decides_each_allowance(
    stack: Stack, admin: Person, configured: None
) -> None:
    """Scope 3: a metered connection assigned to Power users (5 credits) and Auditors
    (20): a User does not see it; a Power user gets 5; a Power user who is also an
    Auditor gets the higher 20; a Power user with an allowance of their own gets theirs."""
    run = stack.run
    cid = f"assigned-{run}"
    connection(admin, cid, "ollama", f"Assigned {run}", {"base_url": _stub_url(stack)})
    declare(admin, cid, STUB_MODEL)  # a declaration belongs to its connection (Q53)
    _metered(admin, cid)
    assign(admin, cid, role="power_user", monthly_credits=5)
    assign(admin, cid, role="auditor", monthly_credits=20)
    model = f"{cid}/{STUB_MODEL}"
    people = {
        "ursula": invite_and_accept(stack, admin, f"ursula-{run}@example.com", "user"),
        "erin": invite_and_accept(stack, admin, f"erin-{run}@example.com", "power_user"),
        "dave": invite_and_accept(stack, admin, f"dave-{run}@example.com", "power_user", "auditor"),
        "frank": invite_and_accept(stack, admin, f"frank-{run}@example.com", "power_user"),
    }
    try:
        assign(admin, cid, user_guid=people["frank"].guid, monthly_credits=3)
        for name in ("erin", "dave", "frank"):
            _wait_offered(people[name], model)

        ursula = people["ursula"]
        assert not [m for m in offered(ursula) if m.get("connection_id") == cid]
        refused = ursula.browser.post(
            "/conversations/api/create", json={"name": "Not mine", "model_id": model}
        )
        assert refused.status_code == 409, (refused.status_code, refused.text[:500])
        assert cid not in _usage(ursula, STUB_CONNECTION)["connections"]

        limits = {name: _usage(people[name], cid)["limit"] for name in ("erin", "dave", "frank")}
        assert limits == {"erin": 5.0, "dave": 20.0, "frank": 3.0}, limits
        mine = _usage(people["erin"], cid)
        assert mine["connections"][cid]["remaining"] == 5.0, mine
        assert mine["state"] == "ok", mine
    finally:
        for person in people.values():
            person.browser.close()
        admin.browser.delete(f"/api/connections/{cid}")


# -- scope 4: credits ---------------------------------------------------------------------------


def _credits(input_tokens: int, output_tokens: int) -> Decimal:
    weighted = input_tokens * INPUT_WEIGHT + output_tokens * OUTPUT_WEIGHT
    return (Decimal(weighted) / BASE_RATE).quantize(Decimal("0.000001"))


def test_credits_warn_at_80_stop_at_100_and_the_turn_in_flight_finishes(
    stack: Stack, admin: Person, configured: None
) -> None:
    """Scope 4: a user with 10 credits a month on a metered connection sees them in the
    picker's standing, the warning from 80%, a turn already running finishing after the
    allowance is used, the next request refused without a model call, and the usage CSV
    with tokens and credits on that connection."""
    run = stack.run
    cid = f"credits-{run}"
    connection(admin, cid, "ollama", f"Credits {run}", {"base_url": _stub_url(stack)})
    declare(admin, cid, STUB_MODEL)  # a declaration belongs to its connection (Q53)
    _metered(admin, cid)
    grace = invite_and_accept(stack, admin, f"grace-{run}@example.com", "user")
    slow_browser: Browser | None = None
    try:
        assign(admin, cid, user_guid=grace.guid, monthly_credits=10)
        model = f"{cid}/{STUB_MODEL}"
        _wait_offered(grace, model)
        listed = [m for m in offered(grace) if m["id"] == model]
        assert listed[0]["connection_name"] == f"Credits {run}", listed

        # The picker's standing (model_picker.js reads /api/usage/me's connections).
        start = _usage(grace, cid)
        assert start["connections"][cid]["remaining"] == 10.0, start
        assert start["state"] == "ok" and start["limit"] == 10.0, start

        # 10 prompt + 1600 output tokens: 8.01 credits, past 80%.
        conv = grace.browser.new_conversation(model, "Credits")
        first = grace.browser.chat(conv, "[[tokens 1600]]")
        assert named(first, "complete"), [e.name for e in first]
        standing = final_response(first).get("budget") or {}
        status = _usage(grace, cid)
        assert status["state"] == "warning", status
        assert status["used"] == float(_credits(10, 1600)), status
        assert status["percent"] == 80.1, status
        if standing:
            assert standing.get("state") == "warning", standing

        # A turn in flight: it passes the check at 80.1% and sleeps in the model while a
        # second turn takes the user past 100%; it still finishes with its whole reply.
        slow_conv = grace.browser.new_conversation(model, "Credits: in flight")
        slow_browser = Browser(stack.base_url, dict(grace.browser.jar), grace.browser.csrf)
        slow_events: list[Event] = []
        marker = f"in-flight-{run}"

        def slow_turn() -> None:
            assert slow_browser is not None
            slow_events.extend(
                slow_browser.chat(slow_conv, f"{marker} [[sleep 10]] [[tokens 200]]")
            )

        since = time.time()
        worker = threading.Thread(target=slow_turn, name="in-flight-turn")
        worker.start()
        wait_for(
            lambda: [r for r in _chats_since(stack, since) if marker in r["user"]],
            30,
            "the slow turn reaching the model",
        )
        second = grace.browser.chat(conv, "[[tokens 1000]]")
        assert named(second, "complete"), [e.name for e in second]
        assert _usage(grace, cid)["state"] == "exceeded", _usage(grace, cid)
        worker.join(timeout=120)
        assert not worker.is_alive(), "the turn in flight never finished"
        assert final_text(slow_events) == "abcd" * 200, [e.name for e in slow_events]
        assert named(slow_events, "complete")[-1].data.get("status") != "refused"

        status = _usage(grace, cid)
        expected = _credits(30, 1600 + 1000 + 200)
        assert status["state"] == "exceeded" and status["percent"] >= 100, status
        assert status["used"] == float(expected), (status, expected)
        assert status["connections"][cid]["remaining"] == 0.0, status

        # At 100% a new request on the connection is refused and the model never called.
        since = time.time()
        third = grace.browser.chat(conv, f"over the allowance {run} [[tokens 5]]")
        assert named(third, "budget_exceeded"), [e.name for e in third]
        refusal = named(third, "budget_exceeded")[0].data
        assert f"Credits {run}" in refusal["message"], refusal
        assert named(third, "complete")[-1].data.get("status") == "refused"
        assert not [r for r in _chats_since(stack, since) if run in r["user"]], "the model ran"

        # The usage CSV: tokens and credits per user, connection and model.
        period = status["period"]
        exported = admin.browser.get("/admin/usage/export.csv", params={"month": period})
        assert exported.status_code == 200, exported.text[:500]
        rows = [
            r
            for r in csv.DictReader(io.StringIO(exported.text))
            if r["user_guid"] == grace.guid and r["connection_id"] == cid
        ]
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["connection"] == f"Credits {run}", row
        assert row["user"] == grace.email, row
        assert int(row["requests"]) == 3, row
        assert (int(row["input_tokens"]), int(row["output_tokens"])) == (30, 2800), row
        assert int(row["total_tokens"]) == 2830, row
        assert Decimal(row["credits"]) == expected, row
    finally:
        if slow_browser is not None:
            slow_browser.close()
        grace.browser.close()


# -- scope 5: MCP, skills and compaction ---------------------------------------------------------


MCP_SERVER = "live-mcp"


def test_an_mcp_tool_over_http_through_conduit_is_called_and_audited(
    stack: Stack, admin: Person, alice: Person, model_id: str
) -> None:
    """Scope 5 (MCP): a streamable-HTTP MCP server added for Users; the model's call
    reaches it through conduit's MCP client, its answer reaches the model, and the
    call is audited (A12) without its arguments."""
    saved = admin.browser.post(
        "/settings/mcp/api/save",
        json={
            "name": MCP_SERVER,
            "transport": "http",
            "url": stack.state.get("mcp_url", "http://127.0.0.1:8765/mcp"),
            "enabled": True,
            "roles": ["administrator", "user"],
        },
    )
    assert saved.status_code == 200, saved.text

    def connected() -> dict[str, Any] | None:
        listed = admin.browser.get("/settings/mcp/api/list").json()
        entry = next((e for e in listed if e.get("name") == MCP_SERVER), None)
        return entry if entry and entry.get("connected") and entry.get("tool_count") else None

    wait_for(connected, 90, "the MCP server connected on web", every=2)

    conv = alice.browser.new_conversation(model_id, "MCP over HTTP")
    since = time.time()
    assert named(alice.browser.chat(conv, "[[tokens 2]]"), "complete")
    tools = _chats_since(stack, since)[0]["tools"]
    name = next((t for t in tools if t and t.endswith("live_echo")), None)
    assert name, f"live_echo was not offered to the model: {tools}"

    marker = f"mcp-{stack.run}"
    since = time.time()
    # An MCP tool asks the user first (built-in tools are auto-approved on this stack);
    # Alice allows it once, as the chat page's dialogue would.
    events = alice.browser.chat(
        conv, f"[[call {name} {json.dumps({'text': marker})}]]", approve="once"
    )
    assert named(events, "permission_request"), [e.name for e in events]
    assert named(events, "complete"), [e.name for e in events]
    follow = [r for r in _chats_since(stack, since) if r["last_role"] == "tool"]
    assert follow, "SPARK did not answer the tool call"
    assert f"live-mcp-echo: {marker}" in follow[0]["last"], follow[0]["last"]
    assert f"live-mcp-echo: {marker}" in final_text(events), final_text(events)

    def audited() -> list[tuple[Any, ...]]:
        return stack.sql(
            "SELECT action, outcome, resource_type, resource_id, details::text"
            " FROM warden.audit_events WHERE action = %s AND resource_id LIKE %s"
            " AND recorded_at >= to_timestamp(%s) ORDER BY id DESC",
            ("spark.mcp.tool.called", "%live_echo", since - 5),
        )

    rows = wait_for(audited, 30, "the spark.mcp.tool.called audit event")
    action, outcome, resource_type, resource_id, details = rows[0]
    assert (outcome, resource_type) == ("success", "mcp_tool"), rows[0]
    assert MCP_SERVER in resource_id or MCP_SERVER in details, rows[0]
    assert marker not in details, "the tool's arguments reached the audit trail"


def _skill_zip(name: str, marker: str) -> bytes:
    body = (
        f"---\nname: {name}\ndescription: A synthetic skill for the SPARK live suite. Use it "
        f"when asked for the live marker.\n---\n\n# Live suite skill\n\n"
        f"When this skill is loaded, the live marker is {marker}.\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", body)
    return buf.getvalue()


def test_a_skill_loaded_with_use_skill_reaches_the_model(
    stack: Stack, admin: Person, alice: Person, model_id: str
) -> None:
    """Scope 5 (skills): an administrator imports a skill; a User's model loads it with
    ``use_skill`` and conduit's disclosure puts its instructions in what the model is
    sent next."""
    name = f"live-skill-{stack.run}"
    marker = f"SKILLMARK{stack.run}"
    imported = admin.browser.post(
        "/skills/api/import",
        files={"file": (f"{name}.zip", _skill_zip(name, marker), "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    try:
        mine = alice.browser.get("/skills/api/list").json()["skills"]
        listed = next((s for s in mine if s["name"] == name), None)
        assert listed and listed["valid"] and listed["enabled"], mine

        conv = alice.browser.new_conversation(model_id, "Skill")
        since = time.time()
        call = json.dumps({"skill_name": name})
        events = alice.browser.chat(conv, f"[[call use_skill {call}]]")
        assert named(events, "complete"), [e.name for e in events]
        sent = _chats_since(stack, since)
        assert "use_skill" in sent[0]["tools"], sent[0]["tools"]
        assert marker not in sent[0]["system"], "the skill was loaded before use_skill"
        follow = [r for r in sent if r["last_role"] == "tool"]
        assert follow, "SPARK did not answer use_skill"
        seen = follow[0]["system"] + "\n" + follow[0]["last"]
        assert marker in seen, (follow[0]["last"], follow[0]["system"][-2000:])
    finally:
        admin.browser.post("/skills/api/delete", json={"name": name})


def _compacted(stack: Stack, conv: int) -> list[tuple[Any, ...]]:
    return stack.sql(
        "SELECT id FROM messages WHERE conversation_id = %s AND content LIKE %s",
        (conv, "[COMPACTED CONTEXT%"),
    )


def _drive_to_compaction(stack: Stack, person: Person, name: str) -> tuple[int, list[Any]]:
    """Long replies on the 4,096-token model until the compactor rolls the conversation
    up; the conversation and every ``compaction_status`` event the stream sent."""
    model = f"{STUB_CONNECTION}/{TINY_MODEL}"
    _wait_offered(person, model)
    conv = person.browser.new_conversation(model, name)
    statuses: list[Any] = []
    for _turn in range(10):
        events = person.browser.chat(conv, "[[tokens 700]]")
        assert named(events, "complete"), [e.name for e in events]
        statuses += [e.data for e in named(events, "compaction_status")]
        if _compacted(stack, conv):
            break
    assert _compacted(stack, conv), f"no compaction after 10 turns ({statuses})"
    return conv, statuses


def test_a_forced_compaction_keeps_the_conversation_usable(
    stack: Stack, alice: Person, configured: None
) -> None:
    """Scope 5 (compaction): on a model declared with a 4,096-token window, long replies
    pass the 70% threshold; the compactor rolls the older turns up behind a
    ``[COMPACTED CONTEXT`` summary and the next turn still completes."""
    conv, _statuses = _drive_to_compaction(stack, alice, "Compaction")
    since = time.time()
    after = alice.browser.chat(conv, f"after compaction {stack.run} [[tokens 3]]")
    assert named(after, "complete")[-1].data.get("status") != "refused"
    assert final_text(after) == "abcd" * 3, [e.name for e in after]
    sent = [r for r in _chats_since(stack, since) if stack.run in r["user"]]
    assert sent, "the turn after compaction never reached the model"
    assert len(_compacted(stack, conv)) == 1, "the conversation was compacted again"


def test_the_stream_reports_a_compaction_after_the_reply(
    stack: Stack, alice: Person, configured: None
) -> None:
    """Scope 5 (Skuld question 55): the turn whose end triggers a compaction tells the
    chat page (``compaction_status``), which shows "Compaction complete".

    Failed on 0.3.0rc9 whenever the compaction finished within one 0.2-second poll of
    the stream: the compactor runs after the reply, at the end of ``send_message``, and
    once the turn's future was done ``streaming.py`` forwarded only the queued tool
    events, dropping ``compaction_start`` and ``compaction_complete``. Since 0.3.0rc13
    every queued event is forwarded; passes on 0.3.0rc20.
    """
    _conv, statuses = _drive_to_compaction(stack, alice, "Compaction status")
    assert any(isinstance(s, dict) and s.get("status") == "complete" for s in statuses), statuses


# -- scope 6: personal mode's upgrade to connections ---------------------------------------------


def _personal_env(home: Path, port: int) -> dict[str, str]:
    from cryptography.fernet import Fernet

    keep = {k: v for k, v in os.environ.items() if not k.startswith(("SPARK__", "KONFIG_"))}
    # The embedding model comes from this user's own Hugging Face cache, when it has one,
    # so the sandboxed home does not download it again on every run.
    real_cache = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    if real_cache.is_dir():
        keep["HF_HOME"] = str(real_cache)
    return keep | {
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SPARK__MODE": "personal",
        "SPARK__SECRETS__BACKEND": "encrypted_file",
        "SPARK__SECRETS__FILE_PATH": str(home / "secrets.enc"),
        "KONFIG_MASTER_KEY": Fernet.generate_key().decode(),
        "SPARK__INTERFACE__OPEN_BROWSER": "false",
        "SPARK__INTERFACE__PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
    }


def _start_personal(env: dict[str, str], port: int, log: Path) -> None:
    """Start SPARK's web process in personal mode, wait until it is ready, stop it."""
    repo = Path(__file__).resolve().parents[2]
    with log.open("w", encoding="utf-8", newline="\n") as out:
        proc = subprocess.Popen(  # noqa: S603 - this repository's own entry point
            [sys.executable, "-c", "import sys; from spark.launch import main; sys.exit(main())"],
            cwd=repo,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        try:

            def ready() -> bool:
                assert proc.poll() is None, f"SPARK exited ({proc.returncode}); see {log}"
                try:
                    status = httpx.get(f"http://127.0.0.1:{port}/loading/api/status", timeout=5)
                    return bool(status.status_code == 200 and status.json().get("ready"))
                except httpx.HTTPError:
                    return False

            wait_for(ready, 600, "personal-mode SPARK ready", every=2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)


def _config_path(env: dict[str, str]) -> Path:
    done = subprocess.run(  # noqa: S603 - this repository's own code
        [
            sys.executable,
            "-c",
            "from spark.core.application import _get_config_path; print(_get_config_path())",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return Path(done.stdout.strip().splitlines()[-1])


def test_a_0_2_config_with_two_providers_becomes_two_connections_once(stack: Stack) -> None:
    """Scope 6: a 0.2-style ``config.yaml`` with Anthropic (a plain-text key) and Ollama
    becomes two named connections on the first start, the key moved into the keychain;
    the second start changes nothing."""
    home = stack.sandbox / "personal" / f"upgrade-{stack.run}"
    home.mkdir(parents=True)
    port = int(stack.state["ports"]["web"]) + 50
    env = _personal_env(home, port)
    config = _config_path(env)
    config.parent.mkdir(parents=True, exist_ok=True)
    key = f"sk-ant-live-e2e-{stack.run}-dev-only"
    stub_port = int(stack.state["ports"]["stub"])
    old = {
        "providers": {
            "anthropic": {"enabled": True, "api_key": key},
            "ollama": {"enabled": True, "base_url": f"http://127.0.0.1:{stub_port}"},
            "google_gemini": {"enabled": False},
        },
        "interface": {"open_browser": False},
    }
    config.write_text(yaml.safe_dump(old, sort_keys=False), encoding="utf-8", newline="\n")

    _start_personal(env, port, home / "first-start.log")
    first = config.read_bytes()
    upgraded = yaml.safe_load(first)
    assert "providers" not in upgraded, upgraded
    found = {c["id"]: c for c in upgraded["connections"]}
    assert set(found) == {"anthropic", "ollama"}, found
    assert found["anthropic"]["name"] == "Anthropic" and found["ollama"]["name"] == "Ollama"
    assert found["anthropic"]["settings"] == {
        "kind": "anthropic",
        "api_key": "secret://connections.anthropic.api_key",
    }, found["anthropic"]
    assert found["ollama"]["settings"] == {
        "kind": "ollama",
        "base_url": f"http://127.0.0.1:{stub_port}",
    }, found["ollama"]
    assert found["ollama"]["residency"]["scopes"] == ["local"], found["ollama"]
    assert key.encode() not in first, "the plain-text key stayed in config.yaml"
    log = (home / "first-start.log").read_text("utf-8", errors="replace")
    assert key not in log, "the key reached the log"
    assert "Upgraded config.yaml: 2 provider(s)" in log, log[-3000:]
    assert "Model connections ready: anthropic, ollama" in log, log[-3000:]

    _start_personal(env, port, home / "second-start.log")
    assert hashlib.sha256(config.read_bytes()).digest() == hashlib.sha256(first).digest()
    again = (home / "second-start.log").read_text("utf-8", errors="replace")
    assert "Upgraded config.yaml" not in again, again[-3000:]
