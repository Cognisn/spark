"""Upgrading to connections (M12).

Personal mode: on the first start after the upgrade, each provider configured in
``config.yaml``'s ``providers`` section becomes a named connection, written once to
its ``connections`` section, and the ``providers`` section is removed. A credential
held as a ``secret://`` reference keeps that reference, so its keychain entry is
used as it is; a credential still in plain text is moved into the keychain under
the connection's own name first. Once ``config.yaml`` has a ``connections``
section the conversion never runs again.

Server mode keeps no providers: 0.3.0 was never released, so the ``providers.*``
instance settings and ``models.offered`` are dropped with no migration. Their
stored rows are left alone and ignored; :func:`ignored_server_settings` lists them
so start-up can say so once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

from spark.connections.model import Connection, default_residency
from spark.connections.store import SECTION
from spark.db.engine import Executor

__all__ = [
    "RETIRED_PREFIXES",
    "SecretsLike",
    "connection_secret",
    "convert_providers",
    "ignored_server_settings",
    "upgrade_config_file",
]

logger = logging.getLogger(__name__)

_SECRET: Final = "secret://"
#: Instance settings that connections replace (dropped with no migration on a server).
RETIRED_PREFIXES: Final = ("providers.", "models.offered")


class SecretsLike(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


def connection_secret(connection_id: str, field: str) -> str:
    """The konfig secret name a connection's credential is kept under."""
    return f"connections.{connection_id}.{field}"


def _enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class _Credentials:
    """Turns a 0.2 provider credential into a reference, moving plain text into konfig."""

    def __init__(self, secrets: SecretsLike) -> None:
        self._secrets = secrets

    def ref(self, value: Any, connection_id: str, field: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        if value.startswith(_SECRET):
            return value
        name = connection_secret(connection_id, field)
        self._secrets.set(name, value)
        logger.info(
            "A plain-text credential for connection %s moved into the keychain", connection_id
        )
        return _SECRET + name


def _anthropic(cfg: Mapping[str, Any], creds: _Credentials) -> dict[str, Any] | None:
    key = creds.ref(cfg.get("api_key"), "anthropic", "api_key")
    return {"kind": "anthropic", "api_key": key} if key else None


def _bedrock(cfg: Mapping[str, Any], creds: _Credentials) -> dict[str, Any] | None:
    cid = "aws-bedrock"
    settings: dict[str, Any] = {"kind": "bedrock", "region": str(cfg.get("region") or "us-east-1")}
    method = str(cfg.get("auth_method") or "sso")
    access = creds.ref(cfg.get("access_key"), cid, "access_key_id")
    secret = creds.ref(cfg.get("secret_key"), cid, "secret_access_key")
    if method in ("iam", "session") and access and secret:
        auth: dict[str, Any] = {
            "mode": "access_keys",
            "access_key_id": access,
            "secret_access_key": secret,
        }
        token = creds.ref(cfg.get("session_token"), cid, "session_token")
        if token:
            auth["session_token"] = token
    else:
        auth = {"mode": "credential_chain"}
        if cfg.get("profile"):
            auth["profile"] = str(cfg["profile"])
    settings["auth"] = auth
    timeout = cfg.get("read_timeout")
    if isinstance(timeout, int | float) and timeout > 0:
        settings["read_timeout_s"] = float(timeout)
    return settings


def _ollama(cfg: Mapping[str, Any], creds: _Credentials) -> dict[str, Any] | None:
    settings: dict[str, Any] = {"kind": "ollama"}
    if cfg.get("base_url"):
        settings["base_url"] = str(cfg["base_url"])
    return settings


def _gemini(cfg: Mapping[str, Any], creds: _Credentials) -> dict[str, Any] | None:
    key = creds.ref(cfg.get("api_key"), "google-gemini", "api_key")
    return {"kind": "gemini", "api_key": key} if key else None


def _xai(cfg: Mapping[str, Any], creds: _Credentials) -> dict[str, Any] | None:
    key = creds.ref(cfg.get("api_key"), "xai", "api_key")
    return {"kind": "openai_compat", "service": {"name": "xai", "api_key": key}} if key else None


_Converter = Callable[[Mapping[str, Any], _Credentials], dict[str, Any] | None]

#: 0.2 provider key -> (connection id, display name, converter), in 0.2's order.
_PROVIDERS: Final[tuple[tuple[str, str, str, _Converter], ...]] = (
    ("anthropic", "anthropic", "Anthropic", _anthropic),
    ("aws_bedrock", "aws-bedrock", "Amazon Bedrock", _bedrock),
    ("ollama", "ollama", "Ollama", _ollama),
    ("google_gemini", "google-gemini", "Google Gemini", _gemini),
    ("xai", "xai", "X.AI", _xai),
)


def convert_providers(providers: Mapping[str, Any], secrets: SecretsLike) -> list[Connection]:
    """The named connections a 0.2 ``providers`` section describes.

    A provider that is enabled, or has a credential, becomes a connection (enabled
    as it was); one without the credential it needs is left out.
    """
    creds = _Credentials(secrets)
    converted: list[Connection] = []
    for key, connection_id, name, convert in _PROVIDERS:
        cfg = providers.get(key)
        if not isinstance(cfg, Mapping):
            continue
        enabled = _enabled(cfg.get("enabled", False))
        has_credential = any(
            isinstance(cfg.get(f), str) and cfg.get(f)
            for f in ("api_key", "access_key", "secret_key")
        )
        if not enabled and not has_credential:
            continue
        settings = convert(cfg, creds)
        if settings is None:
            logger.warning("Provider %s has no credential; not converted to a connection", key)
            continue
        converted.append(
            Connection(
                id=connection_id,
                name=name,
                settings=settings,
                residency=default_residency(settings),
                enabled=enabled,
            )
        )
    return converted


def upgrade_config_file(path: Path, secrets: SecretsLike) -> list[Connection]:
    """Convert ``path``'s providers into connections, once. Returns the connections written.

    Nothing happens when the file already has a ``connections`` section, or has no
    providers.
    """
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or SECTION in raw:
        return []
    providers = raw.get("providers")
    if not isinstance(providers, Mapping):
        return []
    converted = convert_providers(providers, secrets)
    raw[SECTION] = [c.to_record() for c in converted]
    raw.pop("providers", None)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(raw, fh, default_flow_style=False, sort_keys=False)
    logger.info(
        "Upgraded config.yaml: %d provider(s) are now named connections (%s)",
        len(converted),
        ", ".join(c.id for c in converted) or "none",
    )
    return converted


async def ignored_server_settings(db: Executor) -> list[str]:
    """The stored instance settings connections replaced, which a server now ignores."""
    rows = await db.fetch_all(
        "SELECT key FROM instance_settings WHERE key LIKE 'providers.%' OR key = 'models.offered' "
        "ORDER BY key"
    )
    return [str(r["key"]) for r in rows]
