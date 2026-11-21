"""Reasoning reaches the caller as conduit delivers it, before the answer streams.

``ConnectionService.invoke_model`` honours ``reasoning_callback(text)``: each reasoning
block's readable text is passed to it the moment conduit reports the block. A call that
has reported reasoning is not retried, because the caller has already shown it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from conduit import ModelRequest, ProviderError, ProviderFailure, ProviderStateBlock, Usage
from conduit.core.streaming import ProviderStateReported
from conduit.providers.stub import StubToolCall, StubTurn

from spark.connections import service as service_module
from spark.core.conversation_manager import ConversationManager
from tests.model_helpers import stub_connection, stub_runtime

MODEL = "stub/stub-echo"
THOUGHT = "The user wants my name. One line will do."
THOUGHT_2 = "The lookup returned. I can answer now."
ANSWER = "My name is Spark."
MESSAGES = [{"role": "user", "content": "hi"}]


def _state(kind: str, state_type: str, payload: dict[str, Any]) -> ProviderStateBlock:
    return ProviderStateBlock(
        provider_kind=kind, state_type=state_type, payload=json.dumps(payload).encode("utf-8")
    )


def nova(text: str) -> ProviderStateBlock:
    return _state("bedrock", "thinking_text", {"reasoningText": {"text": text}})


def claude(text: str) -> ProviderStateBlock:
    return _state("anthropic", "thinking", {"type": "thinking", "thinking": text, "signature": "s"})


def redacted() -> ProviderStateBlock:
    return _state("anthropic", "redacted_thinking", {"type": "redacted_thinking", "data": "xx"})


def turn(text: str = "", *state: ProviderStateBlock, tool: str | None = None) -> StubTurn:
    calls = (StubToolCall(name=tool, input={}),) if tool else ()
    usage = Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0)
    return StubTurn(text=text, tool_calls=calls, state=tuple(state), usage=usage)


class Script:
    def __init__(self) -> None:
        self.turns: list[StubTurn] = []

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        return self.turns.pop(0) if self.turns else turn("done")


@pytest.fixture()
def rig() -> Iterator[tuple[Any, Script]]:
    script = Script()
    runtime = stub_runtime(connections=[stub_connection(script="live")], scripts={"live": script})
    yield runtime, script
    runtime.close()


def _call(runtime: Any, **kwargs: Any) -> dict[str, Any]:
    service = runtime.service(MODEL)
    return service.invoke_model(MESSAGES, **kwargs)


class TestReasoningCallback:
    def test_reasoning_reaches_the_callback_before_the_first_answer_text(self, rig: Any) -> None:
        runtime, script = rig
        script.turns = [turn(ANSWER, nova(THOUGHT))]
        order: list[tuple[str, str]] = []
        _call(
            runtime,
            stream_callback=lambda t: order.append(("text", t)),
            reasoning_callback=lambda t: order.append(("reasoning", t)),
        )
        assert order[0] == ("reasoning", THOUGHT)
        assert [k for k, _ in order].count("reasoning") == 1
        assert "".join(t for k, t in order if k == "text") == ANSWER

    def test_claude_reasoning_is_decoded_too(self, rig: Any) -> None:
        runtime, script = rig
        script.turns = [turn(ANSWER, claude(THOUGHT))]
        seen: list[str] = []
        _call(runtime, reasoning_callback=seen.append)
        assert seen == [THOUGHT]

    def test_several_blocks_arrive_in_order_one_call_each(self, rig: Any) -> None:
        runtime, script = rig
        script.turns = [turn(ANSWER, claude(THOUGHT), nova(THOUGHT_2))]
        seen: list[str] = []
        _call(runtime, reasoning_callback=seen.append)
        assert seen == [THOUGHT, THOUGHT_2]

    def test_reasoning_around_a_tool_call_each_call_reports_its_own(self, rig: Any) -> None:
        runtime, script = rig
        script.turns = [turn("", nova(THOUGHT), tool="lookup"), turn(ANSWER, nova(THOUGHT_2))]
        seen: list[str] = []
        first = _call(runtime, reasoning_callback=seen.append)
        assert first["tool_use"]
        _call(runtime, reasoning_callback=seen.append)
        assert seen == [THOUGHT, THOUGHT_2]

    def test_redacted_reasoning_and_signatures_give_no_callback(self, rig: Any) -> None:
        runtime, script = rig
        signature = _state("anthropic", "signature", {"signature": "abc"})
        script.turns = [turn(ANSWER, redacted(), signature)]
        seen: list[str] = []
        response = _call(runtime, reasoning_callback=seen.append)
        assert seen == []
        assert sum(b["type"] == "provider_state" for b in response["content_blocks"]) == 2

    def test_no_callback_gives_the_same_response(self, rig: Any) -> None:
        runtime, script = rig
        script.turns = [turn(ANSWER, nova(THOUGHT))]
        with_none = _call(runtime)
        script.turns = [turn(ANSWER, nova(THOUGHT))]
        with_callback = _call(runtime, reasoning_callback=lambda t: None)
        assert with_none == with_callback
        assert with_none["content"] == ANSWER

    def test_a_raising_callback_never_breaks_the_call_or_logs_the_text(
        self, rig: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime, script = rig
        script.turns = [turn(ANSWER, claude(THOUGHT), nova(THOUGHT_2))]
        calls: list[str] = []

        def broken(text: str) -> None:
            calls.append(text)
            raise RuntimeError(f"boom {text}")

        with caplog.at_level(logging.DEBUG):
            response = _call(runtime, reasoning_callback=broken)
        assert response["content"] == ANSWER
        assert calls == [THOUGHT, THOUGHT_2]
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "RuntimeError" in logged
        assert "The user wants" not in logged and "lookup returned" not in logged
        assert not any(r.exc_info for r in caplog.records)


class _FailingProvider:
    """Reports one reasoning block, then fails the way a provider that is down does."""

    def __init__(self, real: Any, *, reason_first: bool) -> None:
        self._real = real
        self._reason_first = reason_first
        self.attempts = 0

    async def stream(self, request: ModelRequest) -> Any:
        self.attempts += 1
        if self._reason_first:
            yield ProviderStateReported(state=nova(THOUGHT))
        raise ProviderError(ProviderFailure.UNAVAILABLE)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class TestRetries:
    def _service(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch, **kw: Any
    ) -> tuple[Any, _FailingProvider]:
        monkeypatch.setattr(service_module.time, "sleep", lambda s: None)
        service = runtime.service(MODEL)
        failing = _FailingProvider(service._host.provider(service.ref.connection_id), **kw)
        monkeypatch.setattr(service._host, "provider", lambda connection_id: failing)
        return service, failing

    def test_a_call_that_reported_reasoning_is_not_retried(
        self, rig: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime, _ = rig
        service, failing = self._service(runtime, monkeypatch, reason_first=True)
        seen: list[str] = []
        with pytest.raises(service_module.ModelCallFailed):
            service.invoke_model(MESSAGES, reasoning_callback=seen.append)
        assert failing.attempts == 1
        assert seen == [THOUGHT]

    def test_a_call_with_no_callback_is_retried_as_before(
        self, rig: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime, _ = rig
        service, failing = self._service(runtime, monkeypatch, reason_first=True)
        with pytest.raises(service_module.ModelCallFailed):
            service.invoke_model(MESSAGES)
        assert failing.attempts == service_module._RETRIES + 1

    def test_a_call_that_reported_nothing_is_still_retried(
        self, rig: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime, _ = rig
        service, failing = self._service(runtime, monkeypatch, reason_first=False)
        seen: list[str] = []
        with pytest.raises(service_module.ModelCallFailed):
            service.invoke_model(MESSAGES, reasoning_callback=seen.append)
        assert failing.attempts == service_module._RETRIES + 1
        assert seen == []


class TestManagerEndToEnd:
    def test_the_after_call_reasoning_event_is_not_sent_twice(self, sync_db: Any) -> None:
        script = Script()
        script.turns = [turn(ANSWER, nova(THOUGHT))]
        runtime = stub_runtime(
            sync_db, connections=[stub_connection(script="live")], scripts={"live": script}
        )
        try:
            mgr = ConversationManager(sync_db, runtime, max_tool_iterations=5, user_guid="u1")
            mgr._get_vector_index = lambda conversation_id, user_guid: None  # type: ignore[method-assign]
            cid = mgr.create_conversation("Synthetic", MODEL, "u1")
            order: list[str] = []

            def status(event: str, data: dict[str, Any]) -> None:
                if event == "reasoning":
                    order.append(data["text"])

            mgr.send_message(
                cid,
                "hi",
                "u1",
                status_callback=status,
                stream_callback=lambda t: order.append("<text>"),
            )
            assert order[0] == THOUGHT
            assert order.count(THOUGHT) == 1
            assert mgr.get_reasoning(cid, "u1")
        finally:
            runtime.close()
