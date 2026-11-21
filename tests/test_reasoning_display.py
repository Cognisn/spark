"""A model's reasoning is shown beside its reply and never as part of it.

conduit delivers reasoning as provider state (Claude's ``thinking``, Bedrock's
``reasoning_content`` and Amazon Nova's ``thinking_text``). SPARK reads its text out,
sends it to the page as a ``reasoning`` event, keeps it beside the message in
``message_reasoning`` and shows it again, collapsed, on reload. It is not part of the
answer text, the stored message, an export, a copy, the index or the next turn's
prompt. conduit's stub provider plays the scripted reasoning: no real provider.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from conduit import ModelRequest, ProviderStateBlock, TextBlock, Usage
from conduit.providers.stub import StubToolCall, StubTurn
from fastapi.testclient import TestClient

from spark.core.conversation_manager import ConversationManager
from spark.core.reasoning import block_reasoning, response_reasoning, strip_reasoning_blocks
from spark.database import conversations as convdb
from spark.database import message_reasoning, messages
from spark.web.endpoints.streaming import _sse_event
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.model_helpers import stub_connection, stub_runtime
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in as sign_in_personal

USER = "u1"
MODEL = "stub/stub-echo"
THOUGHT = "The user is asking for my name. I can answer in one line."
THOUGHT_2 = "The lookup returned a value. I can now answer."
ANSWER = "My name is Spark."


def _state(kind: str, state_type: str, payload: dict[str, Any]) -> ProviderStateBlock:
    return ProviderStateBlock(
        provider_kind=kind,
        state_type=state_type,
        payload=json.dumps(payload).encode("utf-8"),
    )


def nova(text: str) -> ProviderStateBlock:
    """Amazon Nova's reasoning as conduit delivers it."""
    return _state("bedrock", "thinking_text", {"reasoningText": {"text": text}})


def claude(text: str) -> ProviderStateBlock:
    """Claude's reasoning as conduit delivers it (with its signature)."""
    return _state(
        "anthropic",
        "thinking",
        {"type": "thinking", "thinking": text, "signature": "sig-synthetic"},
    )


def _turn(
    text: str = "", *state: ProviderStateBlock, tool: str | None = None, tokens: int = 5
) -> StubTurn:
    calls = (StubToolCall(name=tool, input={}),) if tool else ()
    return StubTurn(
        text=text,
        tool_calls=calls,
        state=tuple(state),
        usage=Usage(
            input_tokens=tokens,
            output_tokens=tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
    )


class Script:
    """Plays turns in order and records every request it was sent."""

    def __init__(self) -> None:
        self.turns: list[StubTurn] = []
        self.requests: list[ModelRequest] = []

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        self.requests.append(request)
        return self.turns.pop(0) if self.turns else _turn("done")


@pytest.fixture()
def rig(sync_db: Any) -> Iterator[tuple[ConversationManager, Script, int]]:
    script = Script()
    runtime = stub_runtime(
        sync_db,
        connections=[stub_connection(script="reasoning")],
        scripts={"reasoning": script},
    )
    mgr = ConversationManager(sync_db, runtime, max_tool_iterations=5, user_guid=USER)
    mgr._get_vector_index = lambda conversation_id, user_guid: None  # type: ignore[method-assign]
    cid = mgr.create_conversation("Synthetic", MODEL, USER)
    yield mgr, script, cid
    runtime.close()


def _events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    seen: list[tuple[str, dict[str, Any]]] = []

    def callback(event: str, data: dict[str, Any]) -> None:
        if event == "reasoning":
            seen.append((event, data))

    return seen, callback


def _stored(mgr: ConversationManager, cid: int) -> list[dict[str, Any]]:
    return mgr.get_messages(cid, USER)


# -- Reading the reasoning out of conduit's state ---------------------------------------


def _block(state: ProviderStateBlock) -> dict[str, Any]:
    return {
        "type": "provider_state",
        "provider_kind": state.provider_kind,
        "state_type": state.state_type,
        "payload": base64.b64encode(state.payload).decode("ascii"),
    }


class TestReadingReasoning:
    def test_nova_claude_and_bedrock_reasoning_text(self) -> None:
        bedrock = _state("bedrock", "reasoning_content", {"reasoningText": {"text": "B"}})
        assert block_reasoning(_block(nova("  N  "))) == "N"
        assert block_reasoning(_block(claude("C"))) == "C"
        assert block_reasoning(_block(bedrock)) == "B"

    def test_redacted_reasoning_and_other_state_have_no_text(self) -> None:
        redacted = _state(
            "anthropic", "redacted_thinking", {"type": "redacted_thinking", "data": "x"}
        )
        redacted_bedrock = _state("bedrock", "reasoning_content", {"redactedContent": "eA=="})
        signature = _state("gemini", "signature", {"signature": "abc"})
        for state in (redacted, redacted_bedrock, signature):
            assert block_reasoning(_block(state)) == ""

    def test_a_block_that_is_not_state_or_is_malformed_has_none(self) -> None:
        assert block_reasoning({"type": "text", "text": "thinking"}) == ""
        assert block_reasoning({"type": "provider_state", "payload": "!!not base64!!"}) == ""
        assert (
            block_reasoning({"type": "provider_state", "payload": base64.b64encode(b"[1]")}) == ""
        )
        assert block_reasoning(None) == ""

    def test_a_responses_reasoning_joins_its_blocks_in_order(self) -> None:
        blocks = [_block(nova("one")), {"type": "text", "text": "x"}, _block(nova("two"))]
        assert response_reasoning(blocks) == "one\n\ntwo"
        assert response_reasoning(None) == ""

    def test_stripping_removes_state_blocks_and_nothing_else(self) -> None:
        blocks = [_block(claude("secret thought")), {"type": "text", "text": "kept"}]
        stripped = strip_reasoning_blocks(json.dumps(blocks))
        assert json.loads(stripped) == [{"type": "text", "text": "kept"}]
        assert "secret thought" not in stripped
        assert strip_reasoning_blocks("plain text") == "plain text"
        assert strip_reasoning_blocks("[not json") == "[not json"
        no_state = json.dumps([{"type": "text", "text": "a"}])
        assert strip_reasoning_blocks(no_state) == no_state


# -- A turn through the manager ---------------------------------------------------------


class TestTurn:
    def test_a_streamed_reply_with_reasoning_then_answer(self, rig: Any) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, nova(THOUGHT))]
        events, callback = _events()
        streamed: list[str] = []

        result = mgr.send_message(
            cid,
            "What is your name?",
            USER,
            status_callback=callback,
            stream_callback=streamed.append,
        )

        assert events == [("reasoning", {"text": THOUGHT})]
        assert result["content"] == ANSWER
        assert THOUGHT not in "".join(streamed)
        assert ANSWER in "".join(streamed)
        [_, reply] = _stored(mgr, cid)
        assert reply["content"] == ANSWER
        assert mgr.get_reasoning(cid, USER) == {reply["id"]: THOUGHT}

    def test_claude_reasoning_is_read_the_same_way(self, rig: Any) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, claude(THOUGHT))]
        events, callback = _events()
        mgr.send_message(cid, "hi", USER, status_callback=callback)
        assert events == [("reasoning", {"text": THOUGHT})]
        assert mgr.get_reasoning(cid, USER) == {_stored(mgr, cid)[-1]["id"]: THOUGHT}

    def test_reasoning_at_the_start_and_between_tool_calls(self, rig: Any) -> None:
        mgr, script, cid = rig
        script.turns = [
            _turn("", nova(THOUGHT), tool="no_such_tool"),
            _turn("", nova(THOUGHT_2), tool="no_such_tool"),
            _turn(ANSWER),
        ]
        events, callback = _events()

        result = mgr.send_message(cid, "look it up", USER, status_callback=callback)

        assert [e[1]["text"] for e in events] == [THOUGHT, THOUGHT_2]
        assert result["content"] == ANSWER
        kept = mgr.get_reasoning(cid, USER)
        assistant = [m for m in _stored(mgr, cid) if m["role"] == "assistant"]
        assert [kept.get(m["id"]) for m in assistant] == [THOUGHT, THOUGHT_2, None]
        assert assistant[-1]["content"] == ANSWER

    def test_a_reply_without_reasoning_has_none(self, rig: Any) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER)]
        events, callback = _events()
        mgr.send_message(cid, "hi", USER, status_callback=callback)
        assert events == []
        assert mgr.get_reasoning(cid, USER) == {}

    def test_reasoning_a_service_reports_live_is_not_sent_twice(self, rig: Any) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, nova(THOUGHT))]
        events, callback = _events()
        real = mgr._turn_service

        def with_live_reasoning(*args: Any, **kwargs: Any) -> Any:
            service = real(*args, **kwargs)
            invoke = service.invoke_model

            def invoke_model(msgs: Any, **kw: Any) -> dict[str, Any]:
                kw["reasoning_callback"](THOUGHT)
                return invoke(msgs, **kw)

            service.invoke_model = invoke_model
            return service

        mgr._turn_service = with_live_reasoning  # type: ignore[method-assign]
        mgr.send_message(cid, "hi", USER, status_callback=callback)
        assert events == [("reasoning", {"text": THOUGHT})]

    def test_the_next_prompt_excludes_the_reasoning(self, rig: Any) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, nova(THOUGHT)), _turn("Fine.")]
        mgr.send_message(cid, "What is your name?", USER)
        mgr.send_message(cid, "And how are you?", USER)

        second = script.requests[-1]
        sent_text = " ".join(
            b.text for m in second.messages for b in m.content if isinstance(b, TextBlock)
        )
        assert ANSWER in sent_text
        assert "The user is asking" not in sent_text
        assert not [
            b for m in second.messages for b in m.content if isinstance(b, ProviderStateBlock)
        ]

    def test_reasoning_never_enters_the_answer_text_that_is_indexed_or_counted(
        self, rig: Any
    ) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, nova(THOUGHT))]
        indexed: list[str] = []
        mgr._index_message = lambda c, u, i, role, text: indexed.append(text)  # type: ignore[method-assign]
        mgr.send_message(cid, "hi", USER)
        assert ANSWER in indexed and not [t for t in indexed if "user is asking" in t]
        [_, reply] = _stored(mgr, cid)
        assert reply["token_count"] <= len(ANSWER)  # the answer's tokens, not the reasoning's

    def test_reasoning_is_removed_with_its_message(self, rig: Any, sync_db: Any) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, nova(THOUGHT))]
        mgr.send_message(cid, "hi", USER)
        reply = _stored(mgr, cid)[-1]
        sync_db.call(messages.delete_message, USER, reply["id"], cid)
        assert mgr.get_reasoning(cid, USER) == {}
        assert sync_db.fetch_all("SELECT * FROM message_reasoning") == []

    def test_another_users_reasoning_is_not_readable_or_writable(
        self, rig: Any, sync_db: Any
    ) -> None:
        mgr, script, cid = rig
        script.turns = [_turn(ANSWER, nova(THOUGHT))]
        mgr.send_message(cid, "hi", USER)
        reply = _stored(mgr, cid)[-1]
        assert sync_db.call(message_reasoning.get_reasoning, "someone-else", cid) == {}
        sync_db.call(message_reasoning.add_reasoning, "someone-else", cid, reply["id"], "stolen")
        assert mgr.get_reasoning(cid, USER) == {reply["id"]: THOUGHT}


# -- The stream, the history and the export --------------------------------------------


class TestStreamEvent:
    def test_the_reasoning_event_reaches_the_page(self) -> None:
        mapped = _sse_event({"event": "reasoning", "data": {"text": THOUGHT}})
        assert mapped == {"event": "reasoning", "data": json.dumps({"text": THOUGHT})}


@pytest.fixture()
def personal(sqlite_settings: Any) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = USER
        sign_in_personal(c)
        yield c


def _web_turn(client: TestClient, *turns: StubTurn) -> tuple[ConversationManager, int]:
    db = client.app.state.sync_db  # type: ignore[attr-defined]
    script = Script()
    script.turns = list(turns)
    runtime = stub_runtime(
        db, connections=[stub_connection(script="reasoning")], scripts={"reasoning": script}
    )
    mgr = ConversationManager(db, runtime, max_tool_iterations=5, user_guid=USER)
    mgr._get_vector_index = lambda conversation_id, user_guid: None  # type: ignore[method-assign]
    client.app.state.conversation_manager = mgr  # type: ignore[attr-defined]
    client.app.state.connections = runtime  # type: ignore[attr-defined]
    cid = db.call(convdb.create_conversation, "Synthetic", MODEL, USER)
    mgr.send_message(cid, "What is your name?", USER)
    return mgr, int(cid)


class TestReload:
    def test_the_history_carries_each_replys_reasoning_beside_its_message(
        self, personal: TestClient
    ) -> None:
        _, cid = _web_turn(personal, _turn("", nova(THOUGHT), tool="no_such_tool"), _turn(ANSWER))
        history = personal.get(f"/chat/{cid}/api/history").json()
        with_thought = [m for m in history if m.get("reasoning")]
        assert [m["reasoning"] for m in with_thought] == [THOUGHT]
        assert with_thought[0]["role"] == "assistant"
        assert history[-1]["content"] == ANSWER and "reasoning" not in history[-1]

    def test_the_stored_message_itself_holds_no_reasoning_text(self, personal: TestClient) -> None:
        mgr, cid = _web_turn(personal, _turn(ANSWER, nova(THOUGHT)))
        assert THOUGHT not in json.dumps(mgr.get_messages(cid, USER))


class TestExport:
    FORMATS = ("markdown", "json", "csv", "html")

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_an_export_excludes_the_reasoning(self, personal: TestClient, fmt: str) -> None:
        # A tool-use message keeps Claude's reasoning as provider state; the export drops it
        _, cid = _web_turn(
            personal,
            _turn("", claude(THOUGHT), tool="no_such_tool"),
            _turn(ANSWER, nova(THOUGHT_2)),
        )
        body = personal.get(f"/chat/{cid}/api/export?format={fmt}").text
        encoded = base64.b64encode(claude(THOUGHT).payload).decode("ascii")
        assert ANSWER in body
        for leaked in (THOUGHT, THOUGHT_2, encoded, "sig-synthetic", "provider_state"):
            assert leaked not in body

    def test_the_csv_export_reads_back_as_plain_rows(self, personal: TestClient) -> None:
        _, cid = _web_turn(personal, _turn(ANSWER, nova(THOUGHT)))
        rows = list(
            csv.reader(io.StringIO(personal.get(f"/chat/{cid}/api/export?format=csv").text))
        )
        assert rows[-1][1:] == ["assistant", ANSWER]
