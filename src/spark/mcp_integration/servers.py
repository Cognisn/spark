"""MCP server entries: transports, names, role grants and credentials (A12, A9).

An entry is one item of the ``mcp.servers`` setting. Personal mode keeps every
transport, stdio included, and reads ``config.yaml``. Server mode:

* allows HTTP and SSE only: a stdio server runs a command on the host, so it is
  refused when configured (:func:`validate_entry`) and an existing one is
  ignored with a warning (:func:`stored_entries`);
* takes conduit's rule for the url: ``https``, or plain ``http`` on a loopback
  host only;
* names each server with lower-case letters, digits, ``-`` and ``_``, because the
  name is part of its permission, ``spark.mcp.<server>``;
* grants each server to the roles listed in its ``roles``: a user holds
  ``spark.mcp.<server>`` while one of their roles is listed
  (:func:`grant_permissions`), and a server listing no role is granted to no one;
* keeps credentials (``auth_token``, ``basic_password``, ``custom_headers``,
  ``oauth_client_secret``) out of the entry: they live in konfig's secrets store
  under :func:`secret_name`, and :func:`public_entry` reports only whether each is
  set.

OAuth (``auth_type: oauth``, either mode, HTTP and SSE only) keeps its client id,
pinned issuer and scopes in the entry (:data:`OAUTH_FIELDS`, checked by
:func:`check_oauth` as conduit's ``OAuthAuth`` would); the client secret is a
credential like the others, and the tokens are never in the entry at all
(:mod:`spark.mcp_integration.oauth`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from conduit import SecretRef
from conduit.mcp.settings import OAuthAuth, check_endpoint_url
from pydantic import ValidationError

from spark.mcp_integration.client import MCPServerConfig
from spark.tools.registry import MCP_PERMISSION_PREFIX, mcp_permission_name

__all__ = [
    "OAUTH_FIELDS",
    "PERMISSION_PREFIX",
    "SECRET_FIELDS",
    "SERVER_TRANSPORTS",
    "STDIO_REFUSED",
    "TRANSPORTS",
    "SecretLookup",
    "ServerEntryError",
    "check_oauth",
    "grant_permissions",
    "is_granted",
    "permission_name",
    "public_entry",
    "secret_name",
    "split_secrets",
    "stored_entries",
    "to_config",
    "validate_entry",
]

logger = logging.getLogger(__name__)

TRANSPORTS: Final = ("stdio", "http", "sse")
SERVER_TRANSPORTS: Final = ("http", "sse")
PERMISSION_PREFIX: Final = MCP_PERMISSION_PREFIX
SECRET_FIELDS: Final = ("auth_token", "basic_password", "custom_headers", "oauth_client_secret")
OAUTH_FIELDS: Final = ("oauth_client_id", "oauth_issuer", "oauth_scopes", "oauth_session")
STDIO_REFUSED: Final = (
    "stdio MCP servers run a command on the host and are not available in server mode: "
    "use an HTTP or SSE server"
)
_AUTH_TYPES: Final = ("none", "bearer", "api_key", "basic", "custom", "oauth")
_SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")

# A secrets-store lookup: name -> value, or None when not set.
SecretLookup = Callable[[str], "str | None"]


class ServerEntryError(ValueError):
    """An MCP server entry was refused (the message never contains a credential)."""


def permission_name(server: str) -> str:
    """The permission that grants ``server``: ``spark.mcp.<server>``."""
    return mcp_permission_name(server)


def is_granted(server: str, permissions: Iterable[str] | None) -> bool:
    """Whether ``permissions`` grant ``server`` (never without permissions)."""
    return permissions is not None and permission_name(server) in permissions


def secret_name(server: str, field: str) -> str:
    """The secrets-store name of one credential of ``server``."""
    if field not in SECRET_FIELDS:
        raise ValueError(f"{field} is not an MCP server credential")
    return f"mcp_server_{server}_{field}"


def _known_roles() -> frozenset[str]:
    from spark.identity.permissions import ROLES

    return frozenset(ROLES)


def _roles(name: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise ServerEntryError(f"server {name!r} roles must be a list of role names")
    unknown = sorted(set(value) - _known_roles())
    if unknown:
        raise ServerEntryError(f"server {name!r} names an unknown role: {', '.join(unknown)}")
    return list(dict.fromkeys(value))


def _has_value(value: Any) -> bool:
    return bool(value) if isinstance(value, (str, dict)) else value is not None


def _scopes(name: str, value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise ServerEntryError(f"server {name!r} OAuth scopes must be a list of scope names")
    return [s for s in (v.strip() for v in value) if s]


def check_oauth(entry: Mapping[str, Any], *, secret_set: bool) -> dict[str, Any]:
    """The OAuth fields of ``entry``, checked as conduit's ``OAuthAuth`` checks them.

    ``secret_set`` says whether the server has a client secret (given now, or kept
    in the secrets store). A client secret needs the client id and the issuer it
    pins, so it is only ever sent to the server that issued it. Messages name
    fields, never values. Certificate checks stay on: the OAuth requests share the
    server's HTTP transport, and a code, a token or the client secret must never go
    to an authorisation server that was not verified. Raises :class:`ServerEntryError`.
    """
    name = str(entry.get("name", ""))
    if entry.get("transport") not in SERVER_TRANSPORTS:
        raise ServerEntryError(f"server {name!r}: OAuth needs an HTTP or SSE server")
    if entry.get("ssl_verify", True) is False:
        raise ServerEntryError(f"server {name!r}: OAuth needs Verify SSL on")
    client_id = entry.get("oauth_client_id") or None
    issuer = entry.get("oauth_issuer") or None
    if client_id is not None and not isinstance(client_id, str):
        raise ServerEntryError(f"server {name!r} OAuth client id must be text")
    if issuer is not None and not isinstance(issuer, str):
        raise ServerEntryError(f"server {name!r} OAuth issuer must be a URL")
    client_id = client_id.strip() or None if client_id else None
    issuer = issuer.strip() or None if issuer else None
    scopes = _scopes(name, entry.get("oauth_scopes"))
    if secret_set and client_id is None:
        raise ServerEntryError(
            f"server {name!r}: an OAuth client secret needs the client id it was issued with"
        )
    if secret_set and issuer is None:
        raise ServerEntryError(
            f"server {name!r}: an OAuth client secret needs the issuer it was issued by"
        )
    if issuer is not None:
        reason = check_endpoint_url(issuer)
        if reason is not None:
            raise ServerEntryError(f"server {name!r} OAuth issuer was refused: {reason}")
    refused: str | None = None
    try:
        OAuthAuth(
            client_id=client_id,
            client_secret=SecretRef("spark.mcp.check") if secret_set else None,
            issuer=issuer,
            scopes=tuple(scopes),
        )
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        refused = ", ".join(fields) or "the OAuth settings"
    except ValueError:
        refused = "the OAuth settings"
    if refused is not None:
        raise ServerEntryError(f"server {name!r}: OAuth refused {refused}")
    checked: dict[str, Any] = {
        "oauth_client_id": client_id,
        "oauth_issuer": issuer,
        "oauth_scopes": scopes,
    }
    session = entry.get("oauth_session")
    if isinstance(session, str) and session:
        checked["oauth_session"] = session
    return checked


def validate_entry(entry: Mapping[str, Any], *, server: bool) -> dict[str, Any]:
    """``entry`` checked for the mode, as stored. Raises :class:`ServerEntryError`."""
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ServerEntryError("each server needs a name")
    transport = entry.get("transport")
    if transport not in TRANSPORTS:
        raise ServerEntryError(
            f"server {name!r} transport must be one of {', '.join(SERVER_TRANSPORTS if server else TRANSPORTS)}"
        )
    checked = {k: v for k, v in entry.items() if k not in OAUTH_FIELDS}
    if entry.get("auth_type") == "oauth":
        # Whether a client secret is set is checked where that is known (the endpoint).
        checked.update(check_oauth(entry, secret_set=False))
    if not server:
        return checked
    if transport == "stdio":
        raise ServerEntryError(f"server {name!r}: {STDIO_REFUSED}")
    if _SERVER_NAME.fullmatch(name) is None:
        raise ServerEntryError(
            f"server name {name!r} must be lower-case letters, digits, '-' or '_' "
            "(at most 63 characters)"
        )
    url = entry.get("url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ServerEntryError(f"server {name!r} needs an http or https url")
    # conduit's rule for every MCP endpoint: https, or plain http on this machine only.
    reason = check_endpoint_url(str(url))
    if reason is not None:
        raise ServerEntryError(f"server {name!r} url was refused: {reason}")
    if entry.get("auth_type", "none") not in _AUTH_TYPES:
        raise ServerEntryError(f"server {name!r} auth_type must be one of {', '.join(_AUTH_TYPES)}")
    carried = [f for f in SECRET_FIELDS if _has_value(entry.get(f))]
    if carried:
        raise ServerEntryError(
            f"server {name!r}: {', '.join(carried)} belongs in the secrets store, not the setting"
        )
    for field in ("command", "args", "env", *SECRET_FIELDS):
        checked.pop(field, None)
    checked["roles"] = _roles(name, entry.get("roles"))
    return checked


def stored_entries(value: Iterable[Any], *, server: bool) -> list[Any]:
    """Stored entries usable in this mode, each warned about when it is not.

    On a server a stdio entry, or one this mode refuses, is ignored; a credential
    left in an entry (written before A9 applied to MCP) is dropped, so the
    administrator enters it again into the secrets store. Personal mode keeps
    everything.
    """
    if not server:
        return list(value)
    kept: list[Any] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            kept.append(entry)  # the shape check reports it
            continue
        name = entry.get("name")
        if entry.get("transport") == "stdio":
            logger.warning("Ignoring MCP server %r: %s", name, STDIO_REFUSED.split(":")[0])
            continue
        carried = [f for f in SECRET_FIELDS if _has_value(entry.get(f))]
        if carried:
            logger.warning(
                "Dropping %s of MCP server %r: credentials belong in the secrets store; "
                "enter them again on the instance page",
                ", ".join(carried),
                name,
            )
        cleaned = {k: v for k, v in entry.items() if k not in SECRET_FIELDS}
        try:
            validate_entry(cleaned, server=True)
        except ServerEntryError as exc:
            logger.warning("Ignoring MCP server %r: %s", name, exc)
            continue
        kept.append(cleaned)
    return kept


def grant_permissions(entries: Iterable[Mapping[str, Any]], roles: Iterable[str]) -> frozenset[str]:
    """The ``spark.mcp.<server>`` permissions ``roles`` hold over the enabled HTTP and SSE servers."""
    held = frozenset(roles)
    granted: set[str] = set()
    for entry in entries:
        name = entry.get("name")
        listed = entry.get("roles")
        if (
            isinstance(name, str)
            and entry.get("transport") in SERVER_TRANSPORTS
            and entry.get("enabled", True)
            and isinstance(listed, list)
            and held.intersection(listed)
        ):
            granted.add(permission_name(name))
    return frozenset(granted)


def split_secrets(entry: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """``entry`` without its credentials, and the credentials given (empty ones dropped)."""
    plain = {k: v for k, v in entry.items() if k not in SECRET_FIELDS}
    secrets: dict[str, str] = {}
    for field in SECRET_FIELDS:
        value = entry.get(field)
        if field == "custom_headers" and isinstance(value, dict):
            value = json.dumps(value) if value else ""
        if isinstance(value, str) and value:
            secrets[field] = value
    return plain, secrets


def _headers(name: str, raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring the custom headers of MCP server %r: not JSON", name)
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def to_config(entry: Mapping[str, Any], secrets: SecretLookup | None) -> MCPServerConfig:
    """The client configuration for ``entry``; with ``secrets``, credentials come from the store."""
    name = str(entry.get("name", "unnamed"))
    if secrets is None:
        auth_token = entry.get("auth_token")
        basic_password = entry.get("basic_password")
        custom_headers = entry.get("custom_headers") or {}
    else:
        auth_token = secrets(secret_name(name, "auth_token"))
        basic_password = secrets(secret_name(name, "basic_password"))
        custom_headers = _headers(name, secrets(secret_name(name, "custom_headers")))
    oauth = entry.get("auth_type") == "oauth"
    oauth_secret: Any = None
    if oauth:
        oauth_secret = (
            entry.get("oauth_client_secret")
            if secrets is None
            else secrets(secret_name(name, "oauth_client_secret"))
        )
    return MCPServerConfig(
        name=name,
        transport=str(entry.get("transport", "stdio")),
        enabled=bool(entry.get("enabled", True)),
        command=entry.get("command"),
        args=list(entry.get("args") or []),
        env=dict(entry.get("env") or {}),
        url=entry.get("url"),
        auth_type=str(entry.get("auth_type", "none")),
        auth_token=auth_token,
        auth_header_name=str(entry.get("auth_header_name", "X-API-Key")),
        basic_username=entry.get("basic_username"),
        basic_password=basic_password,
        custom_headers=dict(custom_headers),
        timeout=float(entry.get("timeout", 30)),
        ssl_verify=bool(entry.get("ssl_verify", True)),
        oauth_client_id=(entry.get("oauth_client_id") or None) if oauth else None,
        oauth_client_secret=(str(oauth_secret) if oauth_secret else None),
        oauth_issuer=(entry.get("oauth_issuer") or None) if oauth else None,
        oauth_scopes=_scopes(name, entry.get("oauth_scopes")) if oauth else [],
        oauth_session=(entry.get("oauth_session") or None) if oauth else None,
    )


def public_entry(entry: Mapping[str, Any], secrets: SecretLookup) -> dict[str, Any]:
    """``entry`` as the settings page may see it: no credential, only whether each is set."""
    hidden = (*SECRET_FIELDS, "env", "command", "args", "oauth_session")
    shown = {k: v for k, v in entry.items() if k not in hidden}
    name = str(entry.get("name", ""))
    shown["secrets_set"] = {f: bool(secrets(secret_name(name, f))) for f in SECRET_FIELDS}
    return shown
