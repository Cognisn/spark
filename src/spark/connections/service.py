"""A model on a connection, as SPARK's synchronous orchestrators call it.

SPARK's orchestrators (the conversation manager, debates, panels, sub-agents,
scheduled actions, knowledge graph builds, compaction) are synchronous, run on
worker threads, and speak SPARK's own transcript format: messages as
``{"role", "content"}`` with text, ``tool_use``, ``tool_result`` and image blocks,
and a response dictionary with ``content``, ``stop_reason``, ``usage``,
``tool_use`` and ``content_blocks``. That format is what SPARK stores, so it stays.

:class:`ConnectionService` maps one call in that format to a conduit
``ModelRequest`` on the connection's provider, and conduit's stream back. Every
call goes through the sync facade on the process's conduit loop
(:class:`conduit.LoopThread`): never from the loop's own thread, which the facade
refuses (``FacadeError``) rather than deadlocking. Text is passed to the caller's
stream callback as it arrives, on the caller's thread.

A call is attributed (:class:`spark.connections.usage.Attribution`) before it is
made, so conduit's ``model_call_finished`` event becomes a usage record for the
right user, connection, model and task. conduit's refusals (an undeclared model,
a model outside the connection's residency policy, an output budget above the
model's limit) surface as :class:`ModelRefused` carrying conduit's reason.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from conduit import (
    CacheBreakpoint,
    ConduitError,
    ConfigurationError,
    FacadeError,
    ImageBlock,
    LoopThread,
    Message,
    MeteredProvider,
    ModelEntry,
    ModelRequest,
    ProviderError,
    ProviderFailure,
    ProviderStateBlock,
    ProviderStateReported,
    StreamStopped,
    TextBlock,
    TextDelta,
    ToolCallFinished,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from conduit.core.types import TOOL_NAME_PATTERN

from spark.connections.refs import ModelRef
from spark.connections.usage import Attribution, UsageSink
from spark.core.reasoning import block_reasoning

__all__ = [
    "ConnectionService",
    "ModelCallFailed",
    "ModelRefused",
    "ModelUnavailable",
    "ServiceHost",
]

logger = logging.getLogger(__name__)

#: Retries of a call the provider could not serve (unavailable or timed out), made
#: only before any of its output reached the caller.
_RETRIES: Final = 2
_RETRY_DELAY_S: Final = 2.0
_MEDIA_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class ModelUnavailable(RuntimeError):
    """The chosen connection or model cannot be used now (not configured, disabled, or failed
    to start)."""


class ModelRefused(RuntimeError):
    """conduit refused the call before reaching the provider; the message is conduit's reason."""


class ModelCallFailed(RuntimeError):
    """The provider failed the call (unavailable, timed out, rejected or refused)."""

    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        reasons = {
            ProviderFailure.UNAVAILABLE: (
                "the model provider is unavailable (it may be rate limited or down)"
            ),
            ProviderFailure.TIMEOUT: "the model provider timed out",
            ProviderFailure.REJECTED: (
                "the model provider rejected the request (check the connection's "
                "credentials and the model)"
            ),
            ProviderFailure.REFUSED: "the model declined to answer",
            ProviderFailure.PROTOCOL: "the model provider's response was malformed",
        }
        super().__init__(reasons.get(failure, f"the model call failed ({failure.value})"))


class ServiceHost(Protocol):
    """What a service needs from the connection runtime."""

    @property
    def loop(self) -> LoopThread: ...

    @property
    def usage(self) -> UsageSink | None: ...

    @property
    def timeout(self) -> float: ...

    def provider(self, connection_id: str) -> MeteredProvider: ...

    def entry(self, ref: ModelRef) -> ModelEntry: ...

    def connection_name(self, connection_id: str) -> str: ...

    def access_info(self, connection_id: str) -> str: ...

    def listed_models(self, connection_id: str) -> list[dict[str, Any]]: ...


# -- Tool names -----------------------------------------------------------------------


def _alias(name: str) -> str:
    """A tool name conduit accepts, the same for the same name every time."""
    if re.fullmatch(TOOL_NAME_PATTERN, name):
        return name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] or "tool"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"t_{safe}_{digest}"


class _Names:
    """Tool names as SPARK knows them, and as conduit is sent them."""

    def __init__(self) -> None:
        self._back: dict[str, str] = {}

    def out(self, name: str) -> str:
        alias = _alias(name)
        self._back[alias] = name
        return alias

    def back(self, alias: str) -> str:
        return self._back.get(alias, alias)


# -- SPARK's format to conduit's -----------------------------------------------------


def _b64(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _text_parts(content: Any) -> tuple[TextBlock | ImageBlock, ...]:
    """A tool result's content as conduit blocks."""
    if content is None:
        return ()
    if isinstance(content, str):
        return (TextBlock(text=content),)
    blocks: list[TextBlock | ImageBlock] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                blocks.append(TextBlock(text=item))
            elif isinstance(item, Mapping):
                if item.get("type") == "image":
                    image = _image(item)
                    if image is not None:
                        blocks.append(image)
                else:
                    blocks.append(TextBlock(text=str(item.get("text", ""))))
        return tuple(blocks)
    return (TextBlock(text=str(content)),)


def _image(block: Mapping[str, Any]) -> ImageBlock | None:
    source = block.get("source")
    if not isinstance(source, Mapping) or source.get("type") != "base64":
        return None
    media_type = source.get("media_type")
    data = _b64(source.get("data"))
    if media_type not in _MEDIA_TYPES or data is None:
        return None
    return ImageBlock(media_type=media_type, data=data)


def _blocks(content: Any, role: str, names: _Names) -> list[Any]:
    if isinstance(content, str):
        return [TextBlock(text=content)] if content else []
    blocks: list[Any] = []
    for item in content if isinstance(content, list) else ():
        if isinstance(item, str):
            if item:
                blocks.append(TextBlock(text=item))
            continue
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            text = str(item.get("text", ""))
            if text:
                blocks.append(TextBlock(text=text))
        elif kind == "tool_use":
            raw_input = item.get("input")
            blocks.append(
                ToolUseBlock(
                    id=str(item.get("id", "")),
                    name=names.out(str(item.get("name", ""))),
                    input=dict(raw_input) if isinstance(raw_input, Mapping) else {},
                )
            )
        elif kind == "tool_result":
            blocks.append(
                ToolResultBlock(
                    tool_use_id=str(item.get("tool_use_id", "")),
                    content=_text_parts(item.get("content")),
                    is_error=bool(item.get("is_error", False)),
                )
            )
        elif kind == "image":
            image = _image(item)
            if image is not None:
                blocks.append(image)
        elif kind == "provider_state" and role == "assistant":
            payload = _b64(item.get("payload"))
            if payload is not None:
                blocks.append(
                    ProviderStateBlock(
                        provider_kind=str(item.get("provider_kind", "")),
                        state_type=str(item.get("state_type", "")),
                        payload=payload,
                    )
                )
    return blocks


def to_messages(messages: Sequence[Mapping[str, Any]], names: _Names) -> tuple[Message, ...]:
    """SPARK's messages as conduit's. Empty messages are left out."""
    converted: list[Message] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        blocks = _blocks(message.get("content"), str(role), names)
        if blocks:
            converted.append(Message(role=role, content=tuple(blocks)))
    return tuple(converted)


def to_tools(tools: Sequence[Mapping[str, Any]] | None, names: _Names) -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for tool in tools or ():
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        schema = dict(schema) if isinstance(schema, Mapping) else {}
        schema.setdefault("type", "object")
        specs.append(
            ToolSpec(
                name=names.out(str(tool.get("name", ""))),
                description=str(tool.get("description", "") or ""),
                input_schema=schema,
            )
        )
    return tuple(specs)


# -- conduit's stream to SPARK's response --------------------------------------------


@dataclass
class _Collected:
    blocks: list[dict[str, Any]]
    text: list[str]
    tool_uses: list[dict[str, Any]]
    stop: StreamStopped | None = None
    usage: Usage | None = None
    streamed: bool = False

    def flush(self) -> None:
        if self.text:
            self.blocks.append({"type": "text", "text": "".join(self.text)})
            self.text.clear()


def _response(collected: _Collected) -> dict[str, Any]:
    collected.flush()
    usage = collected.usage or Usage.zero()
    text = "".join(b["text"] for b in collected.blocks if b["type"] == "text")
    stop = collected.stop.stop_reason.value if collected.stop is not None else "end_turn"
    return {
        "content": text,
        "stop_reason": stop,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_tokens,
            "cache_creation_input_tokens": usage.cache_write_tokens,
        },
        "tool_use": collected.tool_uses or None,
        "content_blocks": collected.blocks,
    }


def _estimate(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:  # noqa: BLE001 - an estimate is enough
        return len(text) // 4


class ConnectionService:
    """One model on one connection, called synchronously. Cheap to make: one per turn."""

    def __init__(
        self,
        host: ServiceHost,
        ref: ModelRef,
        *,
        attribution: Attribution | None = None,
    ) -> None:
        self._host = host
        self._ref = ref
        self._attribution = attribution

    # -- what SPARK's orchestrators ask ---------------------------------------------

    @property
    def ref(self) -> ModelRef:
        return self._ref

    @property
    def current_model(self) -> str:
        return str(self._ref)

    @property
    def attribution(self) -> Attribution | None:
        return self._attribution

    def get_provider_name(self) -> str:
        return self._host.connection_name(self._ref.connection_id)

    def get_access_info(self) -> str:
        return self._host.access_info(self._ref.connection_id)

    def list_available_models(self) -> list[dict[str, Any]]:
        return self._host.listed_models(self._ref.connection_id)

    def set_model(self, model: str) -> None:
        """Use ``model`` (a connection and model, or a model on this connection) from now on."""
        parsed = ModelRef.parse(model)
        self._ref = parsed if parsed is not None else ModelRef(self._ref.connection_id, model)

    def supports_streaming(self) -> bool:
        return True

    def count_tokens(self, text: str) -> int:
        return _estimate(text)

    def get_rate_limits(self) -> dict[str, Any] | None:
        return None

    def attributed(self, attribution: Attribution | None) -> ConnectionService:
        """The same model, its calls billed as ``attribution`` says."""
        return ConnectionService(self._host, self._ref, attribution=attribution)

    def with_source(self, source: str) -> ConnectionService:
        """The same model and user, recorded under another task (compaction, say)."""
        if self._attribution is None:
            return self.attributed(None)
        return self.attributed(replace(self._attribution, source=source))

    # -- the call ---------------------------------------------------------------------

    def _request(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int,
        temperature: float | None,
        tools: Sequence[Mapping[str, Any]] | None,
        system: str | None,
        prompt_caching: bool,
        names: _Names,
        correlation: str | None,
        entry: ModelEntry,
    ) -> ModelRequest:
        limit = entry.output_limit or max_tokens
        system_blocks: tuple[TextBlock | CacheBreakpoint, ...] = ()
        if system:
            system_blocks = (TextBlock(text=system),)
            if prompt_caching:
                system_blocks += (CacheBreakpoint(),)
        specs = to_tools(tools, names)
        return ModelRequest(
            model=self._ref.model_id,
            system=system_blocks,
            messages=to_messages(messages, names),
            tools=specs,
            cache_tools=bool(prompt_caching and specs),
            max_output_tokens=max(1, min(int(max_tokens), limit)),
            temperature=temperature,
            timeout_s=min(self._host.timeout, 3_600),
            correlation_id=correlation,
            task_class=self._attribution.source if self._attribution is not None else None,
        )

    def invoke_model(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        stream_callback: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call the model and return SPARK's response dictionary.

        Raises :class:`ModelUnavailable`, :class:`ModelRefused` (conduit's reason) or
        :class:`ModelCallFailed`. A call the provider could not serve is retried
        (twice, with a short wait) only while none of its output has reached the caller;
        reasoning passed to ``reasoning_callback`` counts as output.

        ``reasoning_callback(text)``, when given, is called with each reasoning block's
        readable text as conduit reports it, before the answer that follows it streams.
        """
        if self._host.loop.on_loop_thread():
            # Code on conduit's loop (an event sink, a tool executor) awaits the async API;
            # the sync facade would wait on itself. Refused before any stream is made.
            raise FacadeError(
                "a model service was called on conduit's own loop thread; await the async "
                "API there instead"
            )
        provider = self._host.provider(self._ref.connection_id)
        try:
            entry = self._host.entry(self._ref)
        except ConduitError as refused:
            raise ModelRefused(str(refused)) from None
        usage = self._host.usage
        correlation = (
            usage.attribute(self._attribution)
            if usage is not None and self._attribution is not None
            else None
        )
        names = _Names()
        try:
            request = self._request(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                system=system,
                prompt_caching=bool(kwargs.get("prompt_caching", False)),
                names=names,
                correlation=correlation,
                entry=entry,
            )
            attempt = 0
            while True:
                collected = _Collected(blocks=[], text=[], tool_uses=[])
                try:
                    self._stream(
                        provider,
                        request,
                        collected,
                        names,
                        stream_callback,
                        kwargs.get("reasoning_callback"),
                    )
                    return _response(collected)
                except ProviderError as error:
                    transient = error.failure in (
                        ProviderFailure.UNAVAILABLE,
                        ProviderFailure.TIMEOUT,
                    )
                    if transient and not collected.streamed and attempt < _RETRIES:
                        attempt += 1
                        logger.warning(
                            "Model call on %s failed (%s); retrying (%d of %d)",
                            self._ref.connection_id,
                            error.failure.value,
                            attempt,
                            _RETRIES,
                        )
                        time.sleep(_RETRY_DELAY_S * attempt)
                        continue
                    raise ModelCallFailed(error.failure) from None
                except ConfigurationError as refused:
                    raise ModelRefused(str(refused)) from None
        finally:
            if usage is not None and correlation is not None:
                usage.release(correlation)

    def _stream(
        self,
        provider: MeteredProvider,
        request: ModelRequest,
        collected: _Collected,
        names: _Names,
        stream_callback: Callable[[str], Any] | None,
        reasoning_callback: Callable[[str], Any] | None = None,
    ) -> None:
        # The facade's iterate refuses to run on conduit's own loop thread, too.
        events: Iterator[Any] = self._host.loop.iterate(
            provider.stream(request), timeout=self._host.timeout
        )
        for event in events:
            if isinstance(event, TextDelta):
                collected.text.append(event.text)
                collected.streamed = True
                if stream_callback is not None and event.text:
                    stream_callback(event.text)
            elif isinstance(event, ToolCallFinished):
                collected.flush()
                block = {
                    "type": "tool_use",
                    "id": event.id,
                    "name": names.back(event.name),
                    "input": dict(event.input),
                }
                collected.tool_uses.append(block)
                collected.blocks.append(block)
            elif isinstance(event, ProviderStateReported):
                collected.flush()
                state = event.state
                collected.blocks.append(
                    {
                        "type": "provider_state",
                        "provider_kind": state.provider_kind,
                        "state_type": state.state_type,
                        "payload": base64.b64encode(state.payload).decode("ascii"),
                    }
                )
                if reasoning_callback is not None:
                    self._report_reasoning(collected, reasoning_callback)
            elif isinstance(event, StreamStopped):
                collected.stop = event
                collected.usage = event.usage

    @staticmethod
    def _report_reasoning(collected: _Collected, callback: Callable[[str], Any]) -> None:
        """Pass the block just collected to ``callback`` when it holds readable reasoning.

        Reasoning given to the caller has reached it, so the call is no longer retried.
        A callback that raises never breaks the call; only its exception class is logged,
        never the reasoning.
        """
        text = block_reasoning(collected.blocks[-1])
        if not text:
            return
        collected.streamed = True
        try:
            callback(text)
        except Exception as failure:  # noqa: BLE001 - the caller's hook must not break the call
            logger.warning("A reasoning callback failed (%s)", type(failure).__name__)

    def __repr__(self) -> str:
        return f"ConnectionService({str(self._ref)!r})"
