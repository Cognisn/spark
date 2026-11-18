"""Test-only doubles for AWS: a stubbed Secrets Manager behind ``boto3.client``.

konfig builds its Secrets Manager clients with ``boto3.client("secretsmanager")``
(the settings document and the secrets bundle). :func:`install_secrets_manager`
swaps that for an in-memory service, so a test runs konfig's real layers and
backend with no network and no credentials. Every name, ARN and value here is
invented (account 123456789012, example.com).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

REGION = "ap-southeast-2"
ACCOUNT = "123456789012"
SETTINGS_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:spark/settings-AbCdEf"
BUNDLE_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:spark/secrets-GhIjKl"

PUBLIC = "https://spark.example.com"
OWNER_URL = "postgresql://spark:CHANGEME-owner@db.example.com:5432/spark?sslmode=verify-full"
WEB_URL = "postgresql://spark_web:CHANGEME-web@db.example.com:5432/spark?sslmode=verify-full"
WORKER_URL = (
    "postgresql://spark_worker:CHANGEME-worker@db.example.com:5432/spark?sslmode=verify-full"
)


class _NotFound(Exception):
    """``ResourceNotFoundException``."""


class _Exceptions:
    ResourceNotFoundException = _NotFound


class FakeSecretsManager:
    """The corner of the Secrets Manager API konfig uses, over a dict of SecretStrings."""

    exceptions = _Exceptions

    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets
        self.reads: list[str] = []
        self.writes: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:  # noqa: N803 - boto3's name
        self.reads.append(SecretId)
        if SecretId not in self.secrets:
            raise _NotFound(SecretId)
        return {"SecretString": self.secrets[SecretId]}

    def put_secret_value(self, SecretId: str, SecretString: str) -> dict[str, Any]:  # noqa: N803
        self.writes.append(SecretId)
        self.secrets[SecretId] = SecretString
        return {}


def install_secrets_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: dict[str, Any] | None = None,
    bundle: dict[str, str] | None = None,
) -> FakeSecretsManager:
    """Stub boto3 and point konfig at a settings document and a secrets bundle.

    Sets ``KONFIG_AWS_SETTINGS`` and ``KONFIG_AWS_SECRETS_MANAGER``, clears
    ``KONFIG_MASTER_KEY`` (the environment has none), and returns the service so
    a test can read what was fetched and written.
    """
    import boto3

    service = FakeSecretsManager(
        {
            SETTINGS_ARN: json.dumps(settings) if settings is not None else "",
            BUNDLE_ARN: json.dumps(bundle) if bundle is not None else "",
        }
    )
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: service)
    monkeypatch.setenv("KONFIG_AWS_SETTINGS", SETTINGS_ARN)
    monkeypatch.setenv("KONFIG_AWS_SECRETS_MANAGER", BUNDLE_ARN)
    monkeypatch.delenv("KONFIG_MASTER_KEY", raising=False)
    return service


def environment_settings_document() -> dict[str, Any]:
    """The settings document the test environment holds, and nothing else."""
    return {
        "mode": "server",
        "server": {"public_base_url": PUBLIC},
        "files": {"root": "/var/lib/spark-files"},
        "logging": {"level": "WARNING", "format": "json"},
    }


def environment_bundle() -> dict[str, str]:
    """The secrets bundle: one database URL per login."""
    return {
        "database_url_owner": OWNER_URL,
        "database_url_web": WEB_URL,
        "database_url_worker": WORKER_URL,
    }
