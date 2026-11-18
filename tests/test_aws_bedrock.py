"""Bedrock through the task role: ``credential_chain`` builds its client with no keys.

On ECS the task role's credentials come to boto3 through the container
credentials endpoint (``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI``), found by
boto3's default chain. A Bedrock connection with ``auth.mode: credential_chain``
holds no access key, no secret and no ``secret://`` reference, builds its
``bedrock-runtime`` client from that chain, and never asks SPARK's secrets store
for a credential. Nothing here calls AWS: building a client makes no request.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from conduit import LiteralSecretResolver

from spark.connections import Connection, ConnectionRuntime
from spark.connections.runtime import production_factories, snapshot_of

REGION = "ap-southeast-2"
EXAMPLE_KEY_ID = "AKIAEXAMPLEEXAMPLE1"
EXAMPLE_SECRET = "example-secret-dev-only"  # noqa: S105 - a test value, not a credential


class AskedForNothing:
    """A secret resolver that fails the test if the runtime asks it for anything."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def resolve(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        self.asked.append(repr(args))
        raise AssertionError("a credential-chain connection must not resolve a secret")

    async def aresolve(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.asked.append(repr(args))
        raise AssertionError("a credential-chain connection must not resolve a secret")


def bedrock(cid: str = "aws-bedrock", **auth: Any) -> Connection:
    return Connection(
        id=cid,
        name="Amazon Bedrock",
        settings={
            "kind": "bedrock",
            "region": REGION,
            "auth": {"mode": "credential_chain", **auth},
        },
    )


@pytest.fixture(autouse=True)
def _aws_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A clean AWS environment: no profile, no shared files, no instance metadata lookup."""
    import os

    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none-config"))


@pytest.fixture
def runtime() -> Iterator[ConnectionRuntime]:
    rt = ConnectionRuntime(
        secrets=LiteralSecretResolver({}), factories=production_factories(), drain_s=0.01
    )
    try:
        yield rt
    finally:
        rt.close()


def test_the_connection_validates_with_no_credential_field(runtime: ConnectionRuntime) -> None:
    settings = runtime.validate(bedrock())
    assert settings.kind == "bedrock"
    assert settings.auth.mode == "credential_chain"  # type: ignore[attr-defined]
    assert settings.auth.profile is None  # type: ignore[attr-defined]


def test_the_session_is_built_from_the_default_chain_with_no_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """boto3's Session gets a region and nothing else: no keys, no profile."""
    from conduit.providers.bedrock.factory import BedrockFactory
    from conduit.providers.bedrock.settings import BedrockSettings

    sessions: list[dict[str, Any]] = []
    real = boto3.session.Session

    class Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, **kwargs: Any) -> None:
            sessions.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(boto3.session, "Session", Spy)
    settings = BedrockSettings(region=REGION)
    clients = BedrockFactory().create_client(settings, {})
    try:
        assert sessions == [{"profile_name": None, "region_name": REGION}]
        assert clients.runtime.meta.service_model.service_name == "bedrock-runtime"
        assert clients.control.meta.service_model.service_name == "bedrock"
    finally:
        clients.close()


def test_the_task_role_credentials_reach_the_client_through_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials in the environment (as the chain finds them) sign the client's requests."""
    from conduit.providers.bedrock.factory import BedrockFactory
    from conduit.providers.bedrock.settings import BedrockSettings

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", EXAMPLE_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", EXAMPLE_SECRET)
    clients = BedrockFactory().create_client(BedrockSettings(region=REGION), {})
    try:
        credentials = clients.runtime._request_signer._credentials  # noqa: SLF001
        assert credentials.get_frozen_credentials().access_key == EXAMPLE_KEY_ID
        assert clients.runtime.meta.region_name == REGION
        assert clients.runtime.meta.config.signature_version != "UNSIGNED"
    finally:
        clients.close()


def test_the_runtime_starts_the_connection_without_asking_for_a_secret() -> None:
    resolver = AskedForNothing()
    rt = ConnectionRuntime(secrets=resolver, factories=production_factories(), drain_s=0.01)  # type: ignore[arg-type]
    try:
        rt.apply(snapshot_of([bedrock()]))
        assert rt.failures() == {}
        assert rt.connection("aws-bedrock") is not None
        assert resolver.asked == []
    finally:
        rt.close()


def test_a_chain_connection_with_a_secret_reference_is_not_what_the_guide_describes(
    runtime: ConnectionRuntime,
) -> None:
    """The settings model has no access-key field on credential_chain: keys are another mode."""
    from conduit.core.errors import ConfigurationError

    from spark.connections import ConnectionInvalid

    bad = bedrock(access_key_id="secret://aws-access")
    with pytest.raises((ConnectionInvalid, ConfigurationError)):
        runtime.validate(bad)
