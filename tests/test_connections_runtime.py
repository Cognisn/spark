"""The connection runtime and its services, on conduit's stub provider (no network).

Covers: several connections of one kind side by side; the sync facade (and its
refusal on conduit's own loop); SPARK's message format to conduit and back,
tools and streamed text included; usage recorded with the user, connection,
model, task and four token counts; residency refusals surfaced with conduit's
reason; models offered per connection and per user; live rebuilds that keep a
working provider when a change fails.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from typing import Any, Literal

import pytest
from conduit import (
    FacadeError,
    LiteralSecretResolver,
    LoopThread,
    ProviderSettings,
    ResidencyPolicy,
    ResidencyScope,
    SecretRef,
    TextBlock,
    ToolResultBlock,
)
from conduit.providers.stub import StubFactory, StubSettings, StubToolCall, StubTurn, scripted

from spark.connections import (
    Attribution,
    Connection,
    ConnectionInvalid,
    ConnectionModel,
    ConnectionRuntime,
    ModelRef,
    ModelRefused,
    ModelUnavailable,
    UserScope,
)
from spark.connections.runtime import CatalogueLimits, snapshot_of
from spark.connections.usage import UsageRecord, UsageSink
from spark.database import connections as repo


def stub(cid: str, *, script: str = "echo", enabled: bool = True, **extra: Any) -> Connection:
    return Connection(
        id=cid,
        name=f"Stub {cid}",
        settings={"kind": "stub", "not_for_production": True, "script": script, **extra},
        residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL})),
        enabled=enabled,
    )


ECHO = "stub-echo"


class KeyedSettings(ProviderSettings):
    """A test adapter's settings with a credential (a secret reference)."""

    kind: Literal["keyed"] = "keyed"
    api_key: SecretRef


class KeyedStubFactory:
    """The stub behind settings that carry a credential, for secret-change tests."""

    kind = "keyed"
    settings_model = KeyedSettings

    def create_client(self, settings: ProviderSettings, secrets: Mapping[str, Any]) -> Any:
        assert secrets["api_key"].get_secret_value()
        return None

    def create_provider(
        self, name: str, settings: ProviderSettings, client: Any, *, catalogue: Any = None
    ) -> Any:
        return StubFactory().create_provider(
            name, StubSettings(not_for_production=True), None, catalogue=catalogue
        )


class Recorded:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)


@pytest.fixture
def recorded() -> Recorded:
    return Recorded()


def make_runtime(recorded: Recorded, scripts: dict[str, Any] | None = None) -> ConnectionRuntime:
    return ConnectionRuntime(
        secrets=LiteralSecretResolver({}),
        usage=UsageSink(recorded),
        factories=[StubFactory(scripts=scripts or {})],
        drain_s=0.01,
    )


@pytest.fixture
def runtime(recorded: Recorded) -> Iterator[ConnectionRuntime]:
    rt = make_runtime(recorded)
    try:
        yield rt
    finally:
        rt.close()


def attribution(source: str = "chat") -> Attribution:
    return Attribution(user_guid="u1", source=source, conversation_id=7)


class TestValidation:
    def test_plain_text_credential_refused_by_field_name(self, runtime: ConnectionRuntime) -> None:
        bad = Connection("work", "Work", {"kind": "anthropic", "api_key": "sk-example-not-real"})
        from spark.connections.runtime import production_factories

        validating = ConnectionRuntime(
            secrets=LiteralSecretResolver({}), factories=production_factories()
        )
        try:
            with pytest.raises(ConnectionInvalid) as refused:
                validating.validate(bad)
            assert "api_key" in str(refused.value)
            assert "sk-example" not in str(refused.value)
            good = Connection("work", "Work", {"kind": "anthropic", "api_key": "secret://k"})
            assert validating.validate(good).kind == "anthropic"
        finally:
            validating.close()

    @pytest.mark.parametrize("cid", ["", "Work", "a/b", "-x", "x" * 64])
    def test_bad_ids_refused(self, runtime: ConnectionRuntime, cid: str) -> None:
        with pytest.raises(ConnectionInvalid, match="connection id"):
            runtime.validate(stub("ok").__class__(cid, "N", stub("ok").settings))

    def test_unknown_field_refused(self, runtime: ConnectionRuntime) -> None:
        with pytest.raises(ConnectionInvalid, match="colour"):
            runtime.validate(stub("ok", colour="blue"))


class TestRuntime:
    def test_two_connections_of_one_kind(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("first"), stub("second"), stub("off", enabled=False)]))
        offered = runtime.offered_models()
        assert [m["id"] for m in offered] == ["first/stub-echo", "second/stub-echo"]
        assert offered[0]["connection_name"] == "Stub first"
        first = runtime.service("first/stub-echo", attribution=attribution())
        second = runtime.service("second/stub-echo", attribution=attribution())
        assert first is not None and second is not None
        assert first.invoke_model([{"role": "user", "content": "one"}])["content"] == "one"
        assert second.invoke_model([{"role": "user", "content": "two"}])["content"] == "two"
        assert runtime.service("off/stub-echo") is not None  # resolvable, but not running
        with pytest.raises(ModelUnavailable):
            runtime.service("off/stub-echo").invoke_model(  # type: ignore[union-attr]
                [{"role": "user", "content": "x"}]
            )

    def test_streams_text_and_records_usage(
        self, runtime: ConnectionRuntime, recorded: Recorded
    ) -> None:
        runtime.apply(snapshot_of([stub("work")]))
        service = runtime.service(ModelRef("work", ECHO), attribution=attribution())
        assert service is not None
        pieces: list[str] = []
        response = service.invoke_model(
            [{"role": "user", "content": "hello there from example.com"}],
            system="Be brief.",
            prompt_caching=True,
            stream_callback=pieces.append,
        )
        assert "".join(pieces) == response["content"] == "hello there from example.com"
        assert response["stop_reason"] == "end_turn"
        assert response["usage"]["cache_creation_input_tokens"] > 0
        (record,) = recorded.records
        assert record.connection_id == "work" and record.model_id == ECHO
        assert record.attribution == attribution()
        assert record.output_tokens == response["usage"]["output_tokens"]
        assert record.cache_write_tokens == response["usage"]["cache_creation_input_tokens"]
        # The same prefix again is read from the cache, counted apart.
        service.with_source("compaction").invoke_model(
            [{"role": "user", "content": "again"}], system="Be brief.", prompt_caching=True
        )
        assert recorded.records[1].cache_read_tokens > 0
        assert recorded.records[1].attribution.source == "compaction"
        assert runtime.usage is not None and runtime.usage.pending() == 0

    def test_unattributed_calls_are_not_recorded(
        self, runtime: ConnectionRuntime, recorded: Recorded
    ) -> None:
        runtime.apply(snapshot_of([stub("work")]))
        service = runtime.service("work/stub-echo")
        assert service is not None
        service.invoke_model([{"role": "user", "content": "x"}])
        assert recorded.records == []

    def test_tool_round_trip_in_spark_format(self, recorded: Recorded) -> None:
        def answer(results: tuple[ToolResultBlock, ...]) -> StubTurn:
            (block,) = results[0].content
            assert isinstance(block, TextBlock)
            return StubTurn(text=f"Owner: {block.text}")

        from conduit.providers.stub import answer_tool_results

        from spark.connections.service import _alias

        # The model sees (and calls) the alias conduit accepts.
        alias = _alias("mcp.lookup host")
        script = answer_tool_results(
            StubTurn(
                text="Looking.",
                tool_calls=(StubToolCall(name=alias, input={"ip": "192.0.2.10"}),),
            ),
            answer,
        )
        rt = make_runtime(recorded, {"tools": script})
        try:
            rt.apply(snapshot_of([stub("work", script="tools")]))
            service = rt.service("work/stub-echo", attribution=attribution())
            assert service is not None
            tools = [{"name": "mcp.lookup host", "description": "d", "inputSchema": {}}]
            first = service.invoke_model([{"role": "user", "content": "who?"}], tools=tools)
            assert first["stop_reason"] == "tool_use"
            (call,) = first["tool_use"]
            # SPARK's own tool name comes back, though conduit was sent a safe alias.
            assert call["name"] == "mcp.lookup host" and call["input"] == {"ip": "192.0.2.10"}
            assert [b["type"] for b in first["content_blocks"]] == ["text", "tool_use"]
            history = [
                {"role": "user", "content": "who?"},
                {"role": "assistant", "content": first["content_blocks"]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": "ops@example.com",
                        }
                    ],
                },
            ]
            second = service.invoke_model(history, tools=tools)
            assert second["content"] == "Owner: ops@example.com"
            assert len(recorded.records) == 2
        finally:
            rt.close()

    def test_facade_refuses_conduits_own_loop(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("work")]))
        service = runtime.service("work/stub-echo")
        assert service is not None
        caught: list[BaseException] = []

        async def on_loop() -> None:
            try:
                service.invoke_model([{"role": "user", "content": "x"}])
            except BaseException as exc:  # noqa: BLE001 - the test inspects it
                caught.append(exc)

        runtime.loop.run(on_loop(), timeout=10)
        assert len(caught) == 1 and isinstance(caught[0], FacadeError)

    def test_concurrent_calls_on_threads(
        self, runtime: ConnectionRuntime, recorded: Recorded
    ) -> None:
        runtime.apply(snapshot_of([stub("a"), stub("b")]))
        results: dict[int, str] = {}

        def call(i: int) -> None:
            cid = "a" if i % 2 else "b"
            service = runtime.service(f"{cid}/stub-echo", attribution=attribution())
            assert service is not None
            results[i] = service.invoke_model([{"role": "user", "content": f"m{i}"}])["content"]

        threads = [threading.Thread(target=call, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert results == {i: f"m{i}" for i in range(12)}
        assert len(recorded.records) == 12
        assert {r.connection_id for r in recorded.records} == {"a", "b"}


class TestResidency:
    def test_refusal_surfaces_conduits_reason(self, runtime: ConnectionRuntime) -> None:
        global_only = Connection(
            "work",
            "Work",
            {"kind": "stub", "not_for_production": True},
            residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.GLOBAL})),
        )
        runtime.apply(snapshot_of([global_only]))
        assert runtime.offered_models() == []
        reason = runtime.refusal(ModelRef("work", ECHO))
        assert reason is not None and "scope local is not allowed" in reason
        service = runtime.service(ModelRef("work", ECHO))
        assert service is not None
        with pytest.raises(ModelRefused, match="scope local is not allowed"):
            service.invoke_model([{"role": "user", "content": "x"}])

    def test_undeclared_model_refused(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("work")]))
        service = runtime.service(ModelRef("work", "not-a-model"))
        assert service is not None
        with pytest.raises(ModelRefused, match="not-a-model"):
            service.invoke_model([{"role": "user", "content": "x"}])

    def test_output_budget_clamped_to_the_catalogue(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("work")]))
        service = runtime.service("work/stub-echo")
        assert service is not None
        response = service.invoke_model([{"role": "user", "content": "x"}], max_tokens=10**9)
        assert response["content"] == "x"


class TestModelsAndUsers:
    def test_curated_models_and_tiers(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(
            snapshot_of(
                [stub("work"), stub("off")],
                models=[
                    ConnectionModel("work", ECHO, True, "higher"),
                    ConnectionModel("off", ECHO, False),
                ],
            )
        )
        offered = runtime.offered_models()
        assert [(m["id"], m["tier"]) for m in offered] == [("work/stub-echo", "higher")]
        assert runtime.is_offered("work/stub-echo") and not runtime.is_offered("off/stub-echo")

    def test_assignment_decides_who_sees_a_connection(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(
            snapshot_of(
                [stub("staffed"), stub("personal"), stub("fresh")],
                assignments=[
                    repo.Assignment("staffed", role="staff"),
                    repo.Assignment("personal", user_guid="u9"),
                ],
            )
        )
        staff = UserScope("u1", frozenset({"staff"}))
        owner = UserScope("u9")
        admin = UserScope("u0", frozenset({"administrator"}), administrator=True)
        nobody = UserScope("u2")

        def ids(user: UserScope | None) -> list[str]:
            return [m["connection_id"] for m in runtime.offered_models(user)]

        assert ids(staff) == ["staffed"]
        assert ids(owner) == ["personal"]
        # Unassigned connections are seen by administrators only (M9 before assignment).
        assert ids(admin) == ["fresh"]
        assert ids(nobody) == []
        assert ids(None) == ["fresh", "personal", "staffed"]
        assert runtime.is_offered("staffed/stub-echo", staff)
        assert not runtime.is_offered("staffed/stub-echo", nobody)
        assert not runtime.is_offered("personal/stub-echo", admin)

    def test_bare_model_id_resolves_to_a_connection_the_user_may_use(
        self, runtime: ConnectionRuntime
    ) -> None:
        runtime.apply(
            snapshot_of(
                [stub("a-first"), stub("b-second")],
                assignments=[repo.Assignment("a-first", role="other")],
            )
        )
        assert runtime.resolve(ECHO) == ModelRef("a-first", ECHO)
        admin = UserScope("u1", administrator=True)
        assert runtime.resolve(ECHO, admin) == ModelRef("b-second", ECHO)
        assert runtime.resolve(ECHO, UserScope("u1")) is None
        assert runtime.resolve("unknown-model") is None
        assert runtime.resolve("") is None

    def test_limits_come_from_the_catalogue(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("work")]))
        limits = CatalogueLimits(runtime)
        entry = runtime.catalogue.require("stub", ECHO)
        assert limits.get_context_window("work/stub-echo") == entry.context_window
        assert limits.get_max_output(ECHO) == entry.output_limit
        assert limits.get_context_window("nowhere/unknown") == 8_192

    def test_overlay_adds_a_model(self, runtime: ConnectionRuntime) -> None:
        overlay = repo.OverlayRow(
            "stub", "stub-local-extra", 4_096, "local", output_limit=1_024, tools=True
        )
        runtime.apply(snapshot_of([stub("work")], overlays=[overlay]))
        assert "work/stub-local-extra" in [m["id"] for m in runtime.offered_models()]
        assert runtime.limits("work/stub-local-extra") == (4_096, 1_024)


class TestLiveChanges:
    def test_unchanged_connections_keep_their_provider(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("a"), stub("b")]))
        a, b = runtime.provider("a"), runtime.provider("b")
        runtime.apply(snapshot_of([stub("a"), stub("b", chunk_size=2)]))
        assert runtime.provider("a") is a
        assert runtime.provider("b") is not b

    def test_failed_rebuild_keeps_the_working_provider(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("a")]))
        working = runtime.provider("a")
        runtime.apply(snapshot_of([stub("a", colour="blue")]))  # refused by conduit
        assert runtime.provider("a") is working
        assert "a" in runtime.failures()
        service = runtime.service("a/stub-echo")
        assert service is not None
        assert service.invoke_model([{"role": "user", "content": "still"}])["content"] == "still"

    def test_removed_connection_goes(self, runtime: ConnectionRuntime) -> None:
        runtime.apply(snapshot_of([stub("a"), stub("b")]))
        runtime.apply(snapshot_of([stub("a")]))
        with pytest.raises(ModelUnavailable):
            runtime.provider("b")

    def test_secret_change_rebuilds(self, recorded: Recorded) -> None:
        values = {"keyed.api_key": "one"}

        class Secrets:
            async def resolve(self, ref: Any) -> Any:
                from pydantic import SecretStr

                return SecretStr(values[ref.name])

        rt = ConnectionRuntime(
            secrets=Secrets(), usage=UsageSink(recorded), factories=[KeyedStubFactory()]
        )
        overlay = repo.OverlayRow("keyed", ECHO, 4_096, "local", output_limit=1_024)
        conn = Connection(
            "a",
            "Keyed",
            {"kind": "keyed", "api_key": "secret://keyed.api_key"},
            residency=ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL})),
        )
        try:
            rt.apply(snapshot_of([conn], overlays=[overlay]))
            before = rt.provider("a")
            rt.apply(snapshot_of([conn], overlays=[overlay]))
            assert rt.provider("a") is before  # nothing changed
            values["keyed.api_key"] = "two"
            rt.apply(snapshot_of([conn], overlays=[overlay]))
            assert rt.provider("a") is not before  # the secret changed
            service = rt.service("a/stub-echo")
            assert service is not None
            assert service.invoke_model([{"role": "user", "content": "k"}])["content"] == "k"
        finally:
            rt.close()

    def test_shared_loop_is_not_closed_by_the_runtime(self, recorded: Recorded) -> None:
        loop = LoopThread(name="test-shared")
        rt = ConnectionRuntime(
            secrets=LiteralSecretResolver({}), factories=[StubFactory()], loop=loop
        )
        rt.apply(snapshot_of([stub("a")]))
        rt.close()
        assert loop.run(_answer(), timeout=5) == 42
        loop.close()


async def _answer() -> int:
    return 42


def test_scripted_provider_error_is_retried_then_reported(recorded: Recorded) -> None:
    from conduit import ProviderError, ProviderFailure

    from spark.connections import ModelCallFailed
    from spark.connections import service as service_mod

    flaky = scripted(
        [
            ProviderError(ProviderFailure.UNAVAILABLE),
            StubTurn(text="recovered"),
            ProviderError(ProviderFailure.REJECTED),
        ]
    )
    rt = make_runtime(recorded, {"flaky": flaky})
    old = service_mod._RETRY_DELAY_S
    service_mod._RETRY_DELAY_S = 0.0
    try:
        rt.apply(snapshot_of([stub("a", script="flaky")]))
        service = rt.service("a/stub-echo")
        assert service is not None
        assert service.invoke_model([{"role": "user", "content": "x"}])["content"] == "recovered"
        with pytest.raises(ModelCallFailed, match="rejected"):
            service.invoke_model([{"role": "user", "content": "x"}])
    finally:
        service_mod._RETRY_DELAY_S = old
        rt.close()
