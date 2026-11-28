"""Moving between conversations never stops or loses a running turn.

The sender's page leaves mid-turn (its stream closes) and comes back (it watches the
conversation again), in personal mode (SQLite, the in-process bus) and on a server
(PostgreSQL, the bus over ``pg_notify``):

* the turn keeps running; the returning page is told it is the sender's own turn and
  given its state (its agents, its reply so far, its pending prompts);
* a tool permission prompt raised while the page was away is held and answered on
  return, and every page closes its dialog;
* Stop from the returned page cancels the turn and every agent of it;
* the turn's lock, registries and owner bindings are given back however it ends;
* on a server, another user's prompt, agent or turn id answers 404 and changes
  nothing, and the prompt is sent to no one but the sender;
* the conversations with a running turn of the user's are listed for the marker;
* two app instances on one PostgreSQL: a page served by one replica answers the
  prompt of a turn running on the other, and stops it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations as convdb
from spark.web import interactions
from spark.web.live import bus_for
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_user_scoping_web import _call, _signed_in, client, host, server_db  # noqa: F401
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in as sign_in_personal

LOCAL = "u1"
ADA = "ada@example.com"
GRACE = "grace@example.com"
VIC = "vic@example.com"
FETCH = {"Accept": "application/json"}


def wait_for(check: Callable[[], Any], timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("timed out")


class LongTurn:
    """A conversation manager whose turn starts an agent, writes some text, raises the
    prompts it is told to (when told), and runs until released or stopped."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.ask = threading.Event()
        self.release = threading.Event()
        self.prompts: list[str] = []
        self.fail = False
        self.tool_after_text = False
        self.answers: dict[str, Any] = {}
        self.finished = threading.Event()

    def model_refusal(self, *_: Any) -> None:
        return None

    def send_message(
        self,
        conversation_id: int,
        message: str,
        user_guid: str,
        *,
        status_callback: Any = None,
        stream_callback: Any = None,
        cancel_token: Any = None,
        callbacks: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        try:
            if self.fail:
                raise RuntimeError("the model broke")
            agent = None
            if callbacks is not None:
                agent = callbacks.agent_cancel_register("agent-1")
                status_callback(
                    "agent_start", {"agent_id": "agent-1", "agent_name": "Researcher", "task": "t"}
                )
            if stream_callback is not None:
                stream_callback("Working on it")
            if self.tool_after_text:
                # A tool starts straight after the text: both reach the bus in one flush.
                status_callback(
                    "tool_call", {"tool_use_id": "t1", "tool_name": "web_search", "params": {}}
                )
            self.started.set()
            if self.prompts:
                wait_for(lambda: self.ask.is_set() or cancel_token.is_cancelled(), 60)
            for kind in self.prompts:
                if kind == "permission":
                    self.answers[kind] = callbacks.tool_permission("web_search", {"query": "q"})
                else:
                    self.answers[kind] = callbacks.agent_model(
                        "helper", "task", "model-a", [{"id": "model-a", "name": "A"}], ""
                    )
            wait_for(lambda: self.release.is_set() or cancel_token.is_cancelled(), 60)
            self.answers["agent_cancelled"] = agent is not None and agent.is_cancelled()
            status = "cancelled" if cancel_token.is_cancelled() else "completed"
            return {"status": status, "content": "done", "usage": {}, "tool_calls": []}
        finally:
            self.finished.set()


@dataclass
class World:
    """One app (personal or server), its manager, and the signed-in sender."""

    client: TestClient
    sender: TestClient
    guid: str
    cid: int
    manager: LongTurn
    server: bool
    others: dict[str, tuple[TestClient, str]] = field(default_factory=dict)

    @property
    def app(self) -> Any:
        return self.client.app


def _events(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    event = ""
    for line in lines:
        if line.startswith("event: "):
            event = line[len("event: ") :].strip()
        elif line.startswith("data: ") and event:
            try:
                data = json.loads(line[len("data: ") :])
            except ValueError:
                data = {}
            yield event, data
            event = ""


def leave_mid_turn(browser: TestClient, cid: int, message: str = "hello") -> str:
    """Send a message on the chat stream, read until the turn has started, and leave."""
    url = f"/stream/chat?message={message}&conversation_id={cid}"
    with browser.stream("GET", url) as resp:
        assert resp.status_code == 200
        for event, data in _events(resp.iter_lines()):
            if event == "status":
                return str(data["stream_id"])
    raise AssertionError("no status event")


@contextmanager
def watching(browser: TestClient, cid: int) -> Iterator[Iterator[tuple[str, dict[str, Any]]]]:
    """Watch the conversation (as a returning page does); the events as they come."""
    with browser.stream("GET", f"/stream/conversation?conversation_id={cid}") as resp:
        assert resp.status_code == 200, resp.read()
        yield _events(resp.iter_lines())


def until(events: Iterator[tuple[str, dict[str, Any]]], name: str) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    for event, data in events:
        seen.append((event, data))
        if event == name:
            return seen
    raise AssertionError(f"no {name} event in {seen}")


def turn_is_over(world: World) -> bool:
    app = world.app
    if interactions.record_for_conversation(app, world.cid) is not None:
        return False
    if bus_for(app).turn(world.cid) is not None:
        return False
    if getattr(app.state, "interaction_owners", {}):
        return False
    if world.server:
        row = app.state.sync_db.fetch_one(
            "SELECT 1 FROM conversation_turns WHERE conversation_id = ?", (world.cid,)
        )
        return row is None
    return True


@pytest.fixture
def short_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page counts as open for a moment only, and a prompt seen waits briefly."""
    monkeypatch.setattr(interactions, "PRESENCE_FRESH", 0.3)
    monkeypatch.setattr(interactions, "SEEN_LIMIT", 30.0)
    monkeypatch.setattr(interactions, "AWAY_LIMIT", 60.0)


# -- The two modes --------------------------------------------------------------------------


@pytest.fixture
def personal_world(sqlite_settings: Any) -> Iterator[World]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = LOCAL
        sign_in_personal(c)
        manager = LongTurn()
        app.state.conversation_manager = manager
        cid = app.state.sync_db.call(convdb.create_conversation, "Synthetic", "model-a", LOCAL)
        world = World(c, c, LOCAL, cid, manager, server=False)
        try:
            yield world
        finally:
            manager.release.set()


@pytest.fixture
def server_world(request: pytest.FixtureRequest) -> Iterator[World]:
    app_client_: TestClient = request.getfixturevalue("client")
    ada, guid = _signed_in(app_client_, ADA)
    manager = LongTurn()
    app_client_.app.state.conversation_manager = manager  # type: ignore[attr-defined]
    cid = _call(app_client_, convdb.create_conversation, "Ada's", "model-a", guid)
    world = World(app_client_, ada, guid, cid, manager, server=True)
    try:
        yield world
    finally:
        manager.release.set()


@pytest.fixture(params=["personal", pytest.param("server", marks=pytest.mark.postgres)])
def world(request: pytest.FixtureRequest) -> World:
    world: World = request.getfixturevalue(f"{request.param}_world")
    return world


def _body(world: World, **extra: Any) -> dict[str, Any]:
    return {"conversation_id": world.cid, **extra}


# -- Leaving and coming back ------------------------------------------------------------------


class TestLeaveAndReturn:
    def test_the_turn_keeps_running_and_the_returning_page_attaches(self, world: World) -> None:
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        time.sleep(0.5)  # the page has gone; the turn has not
        assert not world.manager.finished.is_set()
        assert interactions.record_for_stream(world.app, stream_id) is not None

        with watching(world.sender, world.cid) as events:
            ready = until(events, "ready")[-1][1]
            assert ready["me"] == world.guid
            assert ready["turn"]["user_guid"] == world.guid
            assert ready["turn"]["stream_id"] == stream_id
            state = dict(until(events, "turn_state"))["turn_state"]
            assert state["stream_id"] == stream_id
            assert state["agents"] == [{"agent_id": "agent-1", "agent_name": "Researcher"}]
            assert state["prompts"] == []
            text = dict(until(events, "turn_text"))["turn_text"]
            assert text["text"] == "Working on it"
            world.manager.release.set()
            finished = dict(until(events, "turn_finished"))["turn_finished"]
            assert finished["user_guid"] == world.guid and finished["status"] == "completed"
        wait_for(lambda: turn_is_over(world))

    def test_the_reply_so_far_is_what_came_after_the_running_tool_started(
        self, world: World
    ) -> None:
        world.manager.tool_after_text = True
        leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        time.sleep(0.5)
        with watching(world.sender, world.cid) as events:
            state = dict(until(events, "turn_state"))["turn_state"]
            assert state["activity"] == {"kind": "tool", "tool_name": "web_search"}
            # The text before the tool is its own message already, not the reply so far
            assert dict(until(events, "turn_text"))["turn_text"]["text"] == ""
            world.manager.release.set()
            until(events, "turn_finished")
        wait_for(lambda: turn_is_over(world))

    def test_a_prompt_raised_while_away_is_answered_on_return(
        self, world: World, short_prompts: None
    ) -> None:
        world.manager.prompts = ["permission"]
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        time.sleep(0.5)  # the page's presence has lapsed
        world.manager.ask.set()
        request_id = wait_for(
            lambda: next(iter(getattr(world.app.state, "permission_events", {})), None)
        )
        time.sleep(1.0)  # well past the page's presence: still held while away
        assert "permission" not in world.manager.answers

        with watching(world.sender, world.cid) as events:
            until(events, "ready")
            state = dict(until(events, "turn_state"))["turn_state"]
            [prompt] = state["prompts"]
            assert prompt["request_id"] == request_id
            assert prompt["kind"] == "permission" and prompt["tool_name"] == "web_search"
            answered = world.sender.post(
                "/chat/permission/respond",
                json=_body(world, request_id=request_id, decision="once", stream_id=stream_id),
            )
            assert answered.status_code == 200
            closed = dict(until(events, "prompt_closed"))["prompt_closed"]
            assert closed["request_id"] == request_id
            wait_for(lambda: "permission" in world.manager.answers)
            assert world.manager.answers["permission"] == "once"
            world.manager.release.set()
            until(events, "turn_finished")
        wait_for(lambda: turn_is_over(world))

    def test_an_agent_model_approval_waits_for_the_returned_page(
        self, world: World, short_prompts: None
    ) -> None:
        world.manager.prompts = ["model"]
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        time.sleep(0.5)
        world.manager.ask.set()
        request_id = wait_for(
            lambda: next(iter(getattr(world.app.state, "agent_model_events", {})), None)
        )
        with watching(world.sender, world.cid) as events:
            state = dict(until(events, "turn_state"))["turn_state"]
            [prompt] = state["prompts"]
            assert prompt["kind"] == "agent_model" and prompt["suggested_model"] == "model-a"
            answered = world.sender.post(
                "/chat/agent/model-approve",
                json=_body(world, request_id=request_id, model_id="model-a", stream_id=stream_id),
            )
            assert answered.status_code == 200, answered.text
            wait_for(lambda: "model" in world.manager.answers)
            world.manager.release.set()
        wait_for(lambda: turn_is_over(world))

    def test_stop_from_a_returned_page_cancels_the_turn_and_its_agents(self, world: World) -> None:
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        with watching(world.sender, world.cid) as events:
            until(events, "turn_state")
            stopped = world.sender.post("/stream/cancel", json=_body(world, stream_id=stream_id))
            assert stopped.status_code == 200
            finished = dict(until(events, "turn_finished"))["turn_finished"]
            assert finished["status"] == "cancelled"
        assert world.manager.answers["agent_cancelled"] is True
        wait_for(lambda: turn_is_over(world))

    def test_one_agent_can_be_stopped_from_a_returned_page(self, world: World) -> None:
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        record = interactions.record_for_stream(world.app, stream_id)
        assert record is not None
        stopped = world.sender.post(
            "/chat/agent/cancel", json=_body(world, agent_id="agent-1", stream_id=stream_id)
        )
        assert stopped.status_code == 200
        assert record.agents["agent-1"].is_cancelled()
        assert not record.token.is_cancelled()
        world.manager.release.set()
        wait_for(lambda: turn_is_over(world))


class TestEveryEnding:
    def test_completed_with_the_page_still_open(self, world: World) -> None:
        world.manager.release.set()
        text = world.sender.get(f"/stream/chat?message=hi&conversation_id={world.cid}").text
        assert "event: complete" in text
        assert turn_is_over(world)  # given back before the stream closes

    def test_completed_after_the_page_left(self, world: World) -> None:
        leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        world.manager.release.set()
        wait_for(lambda: turn_is_over(world))

    def test_cancelled(self, world: World) -> None:
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        assert (
            world.sender.post("/stream/cancel", json=_body(world, stream_id=stream_id)).status_code
            == 200
        )
        wait_for(lambda: turn_is_over(world))

    def test_failed(self, world: World) -> None:
        world.manager.fail = True
        text = world.sender.get(f"/stream/chat?message=hi&conversation_id={world.cid}").text
        assert "event: error" in text
        wait_for(lambda: turn_is_over(world))

    def test_the_non_streaming_send(self, world: World) -> None:
        world.manager.release.set()
        sent = world.sender.post(f"/chat/{world.cid}/api/send", json={"message": "hi"})
        assert sent.status_code == 200, sent.text
        assert turn_is_over(world)

    def test_a_second_send_while_the_turn_runs_is_refused(self, world: World) -> None:
        leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        busy = world.sender.post(f"/chat/{world.cid}/api/send", json={"message": "again"})
        assert busy.status_code == 409
        assert busy.json()["holder"]["user_guid"] == world.guid
        world.manager.release.set()
        wait_for(lambda: turn_is_over(world))


class TestRunningList:
    def test_the_conversations_with_a_running_turn_of_mine(self, world: World) -> None:
        assert world.sender.get("/stream/running", headers=FETCH).json() == {"conversations": []}
        leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        running = world.sender.get("/stream/running", headers=FETCH)
        assert running.status_code == 200
        assert running.json() == {"conversations": [world.cid]}
        world.manager.release.set()
        wait_for(lambda: turn_is_over(world))
        assert world.sender.get("/stream/running", headers=FETCH).json() == {"conversations": []}


# -- Server mode: the sender's only ------------------------------------------------------------


@pytest.mark.postgres
class TestTheSendersOnly:
    def test_another_users_ids_are_404_and_change_nothing(
        self, server_world: World, short_prompts: None
    ) -> None:

        world = server_world
        grace, _ = _signed_in(world.client, GRACE)
        world.manager.prompts = ["permission"]
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        world.manager.ask.set()
        request_id = wait_for(
            lambda: next(iter(getattr(world.app.state, "permission_events", {})), None)
        )
        refused = [
            grace.post(
                "/chat/permission/respond",
                json=_body(world, request_id=request_id, decision="allowed", stream_id=stream_id),
            ),
            grace.post(
                "/chat/agent/model-approve",
                json=_body(world, request_id=request_id, model_id="model-a", stream_id=stream_id),
            ),
            grace.post(
                "/chat/agent/cancel", json=_body(world, agent_id="agent-1", stream_id=stream_id)
            ),
            grace.post("/stream/cancel", json=_body(world, stream_id=stream_id)),
            grace.post("/stream/cancel", json={"stream_id": stream_id}),
        ]
        assert [r.status_code for r in refused] == [404] * 5
        record = interactions.record_for_stream(world.app, stream_id)
        assert record is not None
        assert not record.token.is_cancelled() and not record.agents["agent-1"].is_cancelled()
        assert "permission" not in world.manager.answers
        assert grace.get("/stream/running", headers=FETCH).json() == {"conversations": []}
        # The sender too is answered 404 for a turn that is not the one running.
        wrong = world.sender.post("/stream/cancel", json=_body(world, stream_id="not-the-turn"))
        assert wrong.status_code == 404
        assert not record.token.is_cancelled()
        world.sender.post("/stream/cancel", json=_body(world, stream_id=stream_id))
        wait_for(lambda: turn_is_over(world))

    def test_a_prompt_reaches_only_the_senders_pages(
        self, server_world: World, short_prompts: None
    ) -> None:

        world = server_world
        vic, vic_guid = _signed_in(world.client, VIC)
        shared = world.sender.post(
            f"/conversations/api/{world.cid}/members",
            json={"user_id": _warden_id(world, VIC), "role": "viewer"},
        )
        assert shared.status_code == 200, shared.text
        world.manager.prompts = ["permission"]
        seen: list[tuple[str, dict]] = []

        def watch_as_vic() -> None:
            with watching(vic, world.cid) as events:
                for item in events:
                    seen.append(item)
                    if item[0] == "turn_finished":
                        return

        watcher = threading.Thread(target=watch_as_vic, daemon=True)
        watcher.start()
        wait_for(lambda: bus_for(world.app).watchers(world.cid) == 1)
        stream_id = leave_mid_turn(world.sender, world.cid)
        assert world.manager.started.wait(20)
        world.manager.ask.set()
        wait_for(lambda: getattr(world.app.state, "permission_events", {}))
        wait_for(lambda: any(e == "tool" and d.get("phase") == "approval" for e, d in seen))
        # Vic may read the conversation, but the turn is not his: no stop, no marker.
        assert vic.post("/stream/cancel", json=_body(world, stream_id=stream_id)).status_code == 404
        assert vic.get("/stream/running", headers=FETCH).json() == {"conversations": []}
        assert world.sender.get("/stream/running", headers=FETCH).json() == {
            "conversations": [world.cid]
        }
        world.sender.post("/stream/cancel", json=_body(world, stream_id=stream_id))
        watcher.join(30)
        names = {e for e, _ in seen}
        assert not names & {"prompt", "prompt_closed", "turn_state", "turn_text", "agent"}
        assert "web_search" not in json.dumps([d for e, d in seen if e != "tool"])
        assert '"q"' not in json.dumps(seen)
        wait_for(lambda: turn_is_over(world))


def _warden_id(world: World, email: str) -> str:
    people = world.sender.get("/conversations/api/people?q=" + email.split("@")[0], headers=FETCH)
    assert people.status_code == 200, people.text
    return str(next(p["id"] for p in people.json() if p["email"] == email))


# -- Two replicas on one PostgreSQL ---------------------------------------------------------------


@pytest.mark.postgres
class TestTwoReplicas:
    def test_a_page_on_one_replica_answers_and_stops_a_turn_on_the_other(
        self,
        server_world: World,
        short_prompts: None,
        tmp_path: Path,
        request: pytest.FixtureRequest,
    ) -> None:
        from tests.identity_web_helpers import another_client, server_client, sign_in, write_config

        world = server_world
        server_db_ = request.getfixturevalue("server_db")
        config = write_config(tmp_path / "replica.yaml", autonomous_actions={"enabled": True})
        with server_client(config, server_db_) as replica:
            replica.app.state.conversation_manager = world.manager  # type: ignore[attr-defined]
            ada_there = another_client(replica)
            assert sign_in(ada_there, ADA).status_code == 303
            remote = bus_for(replica.app)

            # The turn runs on the first replica, and its page leaves...
            world.manager.prompts = ["permission"]
            stream_id = leave_mid_turn(world.sender, world.cid)
            assert world.manager.started.wait(20)
            assert interactions.record_for_stream(replica.app, stream_id) is None
            # ...and Ada comes back on a page the second replica serves.
            with watching(ada_there, world.cid) as events:
                ready = until(events, "ready")[-1][1]
                assert ready["turn"]["stream_id"] == stream_id
                wait_for(lambda: remote.listening.is_set())
                state = dict(until(events, "turn_state"))["turn_state"]
                assert state["agents"] == [{"agent_id": "agent-1", "agent_name": "Researcher"}]
                world.manager.ask.set()
                prompt = dict(until(events, "prompt"))["prompt"]
                answered = ada_there.post(
                    "/chat/permission/respond",
                    json=_body(
                        world, request_id=prompt["request_id"], decision="once", stream_id=stream_id
                    ),
                )
                assert answered.status_code == 200
                wait_for(lambda: "permission" in world.manager.answers)
                assert world.manager.answers["permission"] == "once"
                assert ada_there.get("/stream/running", headers=FETCH).json()["conversations"] == [
                    world.cid
                ]
                stopped = ada_there.post("/stream/cancel", json=_body(world, stream_id=stream_id))
                assert stopped.status_code == 200
                finished = dict(until(events, "turn_finished"))["turn_finished"]
                assert finished["status"] == "cancelled"
            assert world.manager.answers["agent_cancelled"] is True
            wait_for(lambda: turn_is_over(world))
