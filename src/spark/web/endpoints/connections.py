"""The connections screens: named model connections, their models, metering and assignments.

* ``/admin/connections`` (server mode): the administrator's Connections page.
  Personal mode shows the same list, modal and models view in Settings
  (``/settings#section-connections``), without metering and assignment (M3).
* ``/api/connections``: the list, the add and edit modal's services and fields,
  create, edit, enable, disable, rename and delete (refused while a scheduled
  action uses the connection, naming the actions).
* ``/api/connections/{id}/models``: conduit's catalogue models for the
  connection's kind and Region, marked reachable or not by the last discovery
  (``/discover`` runs it through conduit, on a worker thread), refused with
  conduit's reason where the residency policy forbids them, enabled or not and
  tiered; ``/overlays`` declares a model the catalogue lacks (discovered or typed in).
* ``/api/connections/{id}/metering`` and ``/assignments`` (server mode only).

Every route needs ``spark.admin.connections`` (401 signed out, 403 without it); in
personal mode the single local user holds it. State-changing requests pass the
Origin and CSRF checks of :mod:`spark.web.security` like every other.

Secrets are write-only: a credential typed into the modal goes straight to
konfig's secrets store as ``connections.<id>.<field path>`` and the connection
keeps only its ``secret://`` reference (A9); other replicas are told through
:func:`spark.instance_settings.secrets.announce`. A page sees each credential only
as set or not set. Settings are validated through conduit's provider registry
before anything is written, and conduit's refusal (fields only) is shown.

Changes are audited through warden under the instance settings actions, with
the connection as the resource and field names only, never values.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from conduit import ResidencyPolicy
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from spark.connections import discovery
from spark.connections.catalogue import entry_of
from spark.connections.model import (
    CONNECTION_ID_PATTERN,
    SERVICES,
    TIERS,
    Connection,
    ConnectionInvalid,
    ConnectionModel,
    default_residency,
    residency_from_record,
    residency_record,
)
from spark.connections.refs import ModelRef
from spark.connections.store import ConnectionStore, DatabaseConnectionStore, FileConnectionStore
from spark.database import autonomous_actions
from spark.database import connections as repo
from spark.identity import audit
from spark.identity.access import require_permission
from spark.identity.permissions import ADMIN_CONNECTIONS, ROLES
from spark.web.db import database, sync_database

__all__ = ["FORMS", "PAGE_PATH", "SAMPLE_TURN", "Field", "router"]

logger = logging.getLogger(__name__)

PAGE_PATH: Final = "/admin/connections"
API: Final = "/api/connections"
SECRET_PREFIX: Final = "secret://"

administrator = require_permission(ADMIN_CONNECTIONS)
router = APIRouter(dependencies=[Depends(administrator)])

#: The sample turn the metering worked example prices at each tier.
SAMPLE_TURN: Final[Mapping[str, int]] = {
    "input_tokens": 1500,
    "output_tokens": 500,
    "cache_read_tokens": 8000,
    "cache_write_tokens": 1000,
}

NOT_STARTED = "The model connections have not started yet. Try again in a moment."


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


# -- The modal's fields per service ---------------------------------------------------

FieldType = Literal["text", "url", "number", "bool", "select", "secret", "fixed"]


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str
    #: Shown only while these other fields hold one of these values.
    when: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Field:
    """One field of a service's modal, at a dotted path in conduit's settings for its kind.

    A path starting with ``_`` is the modal's own chooser and is not stored; its
    value on edit is ``detect``'s answer (the value whose fields the stored
    settings hold). A ``fixed`` field stores ``default`` while it is shown.
    """

    path: str
    label: str
    type: FieldType = "text"
    required: bool = False
    default: Any = None
    options: tuple[Option, ...] = ()
    when: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    help: str = ""
    placeholder: str = ""
    detect: tuple[str, str, str] | None = None  # (path present?, value if so, value if not)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "options": [
                {"value": o.value, "label": o.label, "when": _when(o.when)} for o in self.options
            ],
            "when": _when(self.when),
            "help": self.help,
            "placeholder": self.placeholder,
        }


def _when(when: Mapping[str, tuple[Any, ...]]) -> dict[str, list[Any]]:
    return {k: list(v) for k, v in when.items()}


_LONG_TERM_HELP = (
    "AWS recommends long-term Bedrock API keys for exploration only. They are refused "
    "unless this is ticked."
)


def _region(required: bool, placeholder: str = "") -> Field:
    return Field(
        "region",
        "Region",
        required=required,
        placeholder=placeholder,
        help=(
            "Where the service processes requests. Residency checks use it."
            if required
            else "Optional. Where the service processes requests, for residency checks."
        ),
    )


def _aws_source(prefix: str, when: Mapping[str, tuple[Any, ...]]) -> tuple[Field, ...]:
    """The AWS credentials a short-term Bedrock API key is generated from."""
    chain = {**when, f"{prefix}.mode": ("credential_chain",)}
    keys = {**when, f"{prefix}.mode": ("access_keys",)}
    return (
        Field(
            f"{prefix}.mode",
            "Generate keys from",
            "select",
            default="credential_chain",
            options=(
                Option("credential_chain", "The AWS credential chain"),
                Option("access_keys", "AWS access keys"),
            ),
            when=when,
        ),
        Field(f"{prefix}.profile", "AWS profile", when=chain, help="Optional named profile."),
        Field(f"{prefix}.access_key_id", "Access key ID", "secret", True, when=keys),
        Field(f"{prefix}.secret_access_key", "Secret access key", "secret", True, when=keys),
        Field(f"{prefix}.session_token", "Session token", "secret", when=keys),
    )


def _bedrock_fields(mantle: bool) -> tuple[Field, ...]:
    long_term = Option(
        "long_term_api_key",
        "Long-term Bedrock API key",
        when={"allow_long_term_api_keys": (True,)},
    )
    short_term = Option("short_term_api_key", "Short-term Bedrock API keys (generated)")
    modes: tuple[Option, ...] = (
        (short_term, long_term)
        if mantle
        else (
            Option("credential_chain", "The AWS credential chain"),
            Option("access_keys", "AWS access keys"),
            short_term,
            long_term,
        )
    )
    default = "short_term_api_key" if mantle else "credential_chain"
    fields: list[Field] = [
        _region(True, "ap-southeast-2"),
        Field(
            "allow_long_term_api_keys",
            "Allow long-term Bedrock API keys",
            "bool",
            default=False,
            help=_LONG_TERM_HELP,
        ),
        Field("auth.mode", "Authentication", "select", default=default, options=modes),
    ]
    if not mantle:
        fields += [
            Field(
                "auth.profile",
                "AWS profile",
                when={"auth.mode": ("credential_chain",)},
                help="Optional named profile.",
            ),
            Field(
                "auth.access_key_id",
                "Access key ID",
                "secret",
                True,
                when={"auth.mode": ("access_keys",)},
            ),
            Field(
                "auth.secret_access_key",
                "Secret access key",
                "secret",
                True,
                when={"auth.mode": ("access_keys",)},
            ),
            Field(
                "auth.session_token",
                "Session token",
                "secret",
                when={"auth.mode": ("access_keys",)},
            ),
        ]
    fields += [
        *_aws_source("auth.source", {"auth.mode": ("short_term_api_key",)}),
        Field(
            "auth.api_key",
            "Bedrock API key",
            "secret",
            True,
            when={"auth.mode": ("long_term_api_key",)},
        ),
        Field(
            "endpoint_url",
            "Endpoint URL",
            "url",
            help="Optional. An https endpoint in place of the regional one (a VPC endpoint).",
        ),
    ]
    return tuple(fields)


def _api_key(path: str = "api_key", **kwargs: Any) -> Field:
    return Field(path, "API key", "secret", True, **kwargs)


_FOUNDRY_AUTH = (
    Option("api_key", "API key"),
    Option("entra_id", "Microsoft Entra ID"),
)

#: The modal's fields for each service in M4, from conduit's settings model for its kind.
FORMS: Final[Mapping[str, tuple[Field, ...]]] = {
    "bedrock": _bedrock_fields(mantle=False),
    "bedrock_mantle": _bedrock_fields(mantle=True),
    "anthropic": (
        _api_key(),
        Field("base_url", "Base URL", "url", help="Optional. A gateway in front of the API."),
        _region(False),
    ),
    "anthropic_foundry": (
        Field(
            "base_url",
            "Resource endpoint",
            "url",
            True,
            placeholder="https://<resource>.services.ai.azure.com/anthropic/",
        ),
        Field(
            "_auth",
            "Authentication",
            "select",
            default="api_key",
            options=_FOUNDRY_AUTH,
            detect=("entra_id", "entra_id", "api_key"),
        ),
        _api_key(when={"_auth": ("api_key",)}),
        Field("entra_id.mode", "", "fixed", default="entra_id", when={"_auth": ("entra_id",)}),
        Field(
            "entra_id.managed_identity_client_id",
            "Managed identity client ID",
            when={"_auth": ("entra_id",)},
            help="Optional. A user-assigned managed identity.",
        ),
        _region(False, "australiaeast"),
    ),
    "openai": (
        _api_key("service.api_key"),
        Field("service.base_url", "Base URL", "url", help="Optional. Defaults to OpenAI's API."),
        Field("service.organization", "Organisation", help="Optional."),
        Field("service.project", "Project", help="Optional."),
        _region(False),
    ),
    "xai": (
        _api_key("service.api_key"),
        Field("service.base_url", "Base URL", "url", help="Optional. Defaults to X.AI's API."),
        _region(False),
    ),
    "openai_foundry": (
        Field(
            "service.endpoint",
            "Resource endpoint",
            "url",
            True,
            placeholder="https://<resource>.openai.azure.com/openai/v1/",
        ),
        Field(
            "service.auth.mode",
            "Authentication",
            "select",
            default="api_key",
            options=_FOUNDRY_AUTH,
        ),
        _api_key("service.auth.api_key", when={"service.auth.mode": ("api_key",)}),
        Field(
            "service.auth.managed_identity_client_id",
            "Managed identity client ID",
            when={"service.auth.mode": ("entra_id",)},
            help="Optional. A user-assigned managed identity.",
        ),
        Field(
            "service.catalogue_provider",
            "Catalogue",
            "select",
            default="openai",
            options=(Option("openai", "OpenAI models"), Option("xai", "Grok models")),
            help="Which catalogue the deployments are declared under.",
        ),
        _region(False, "australiaeast"),
    ),
    "gemini": (
        _api_key(),
        Field("base_url", "Base URL", "url", help="Optional. An https proxy in front of the API."),
        _region(False),
    ),
    "ollama": (
        Field(
            "base_url",
            "Base URL",
            "url",
            default="http://127.0.0.1:11434",
            help="The Ollama server. It may not carry credentials.",
        ),
        Field(
            "num_ctx",
            "Context length",
            "number",
            help="Optional. Caps the context requested from Ollama.",
        ),
        _region(False),
    ),
}


def _get(settings: Mapping[str, Any], path: str) -> Any:
    node: Any = settings
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set(settings: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = settings
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _shown(when: Mapping[str, tuple[Any, ...]], values: Mapping[str, Any]) -> bool:
    return all(values.get(path) in allowed for path, allowed in when.items())


def _secret_name(connection_id: str, path: str) -> str:
    return f"connections.{connection_id}.{path}"


def _references(value: Any) -> set[str]:
    """Every konfig secret name a settings mapping refers to."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for item in value.values():
            found |= _references(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _references(item)
    elif isinstance(value, str) and value.startswith(SECRET_PREFIX):
        found.add(value[len(SECRET_PREFIX) :])
    return found


class FormRefused(ValueError):
    """The modal's values cannot make settings; the message names the field."""


def _plain(fld: Field, raw: Any) -> Any:
    """A non-secret field's value as conduit's settings take it, or None when left blank."""
    if fld.type == "bool":
        return bool(raw) if raw is not None else bool(fld.default)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if fld.type == "number":
        try:
            return int(str(raw).strip())
        except ValueError:
            raise FormRefused(f"{fld.label} must be a whole number") from None
    text = str(raw).strip()
    if fld.type == "select" and text not in {o.value for o in fld.options}:
        raise FormRefused(f"{fld.label}: choose one of the options")
    return text


def form_values(fields: Sequence[Field], values: Mapping[str, Any]) -> dict[str, Any]:
    """The modal's values with each chooser's default where none was given."""
    filled = dict(values)
    for fld in fields:
        if fld.type == "bool":
            filled[fld.path] = bool(values.get(fld.path, fld.default))
        elif fld.type == "select" and not values.get(fld.path):
            filled[fld.path] = fld.default
    return filled


def build_settings(
    service: str,
    connection_id: str,
    values: Mapping[str, Any],
    secrets: Mapping[str, Any],
    existing: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """conduit settings for ``service`` from the modal, and the secrets to write.

    Only the service's own fields are read, so nothing else reaches the settings.
    A secret left blank keeps the reference the connection already holds for it.
    Returns (settings with ``secret://`` references, {secret name: value}).
    """
    spec = SERVICES[service]
    fields = FORMS[service]
    shown = form_values(fields, values)
    settings: dict[str, Any] = copy.deepcopy(dict(spec.fixed))
    settings["kind"] = spec.kind
    to_write: dict[str, str] = {}
    for fld in fields:
        if not _shown(fld.when, shown) or fld.path.startswith("_"):
            continue
        if fld.type == "fixed":
            _set(settings, fld.path, fld.default)
        elif fld.type == "secret":
            typed = secrets.get(fld.path)
            if isinstance(typed, str) and typed.strip():
                name = _secret_name(connection_id, fld.path)
                to_write[name] = typed.strip()
                _set(settings, fld.path, SECRET_PREFIX + name)
            else:
                kept = _get(existing or {}, fld.path)
                if isinstance(kept, str) and kept.startswith(SECRET_PREFIX):
                    _set(settings, fld.path, kept)
        else:
            value = _plain(fld, shown.get(fld.path))
            if value is not None:
                _set(settings, fld.path, value)
    return settings, to_write


def describe_settings(
    service: str, settings: Mapping[str, Any], secret_set: Any
) -> tuple[dict[str, Any], dict[str, bool]]:
    """The modal's values for a stored connection, and each secret only as set or not."""
    values: dict[str, Any] = {}
    secrets: dict[str, bool] = {}
    for fld in FORMS.get(service, ()):
        if fld.detect is not None:
            path, present, absent = fld.detect
            values[fld.path] = present if _get(settings, path) is not None else absent
            continue
        stored = _get(settings, fld.path)
        if fld.type == "secret":
            ref = stored[len(SECRET_PREFIX) :] if isinstance(stored, str) else ""
            secrets[fld.path] = bool(ref) and stored.startswith(SECRET_PREFIX) and secret_set(ref)
        elif fld.type != "fixed" and stored is not None:
            values[fld.path] = stored
    return values, secrets


# -- Request helpers ------------------------------------------------------------------


def _runtime(request: Request) -> Any:
    from spark.web.models import connections

    return connections(request)


def _config_path() -> Path:
    from spark.core.application import _get_config_path

    return _get_config_path()


def _store(request: Request) -> ConnectionStore:
    if _server():
        return DatabaseConnectionStore(database(request))
    return FileConnectionStore(_config_path())


def _secrets(request: Request) -> Any:
    return request.app.state.ctx.secrets


def _secret_set(request: Request) -> Any:
    store = _secrets(request)

    def is_set(name: str) -> bool:
        try:
            return bool(store.get(name))
        except Exception:  # noqa: BLE001 - a store that cannot answer reports not set
            return False

    return is_set


async def _refresh(request: Request) -> None:
    """Bring this process's connection runtime in line with what was just stored (other
    server replicas follow the database notification)."""
    runtime = _runtime(request)
    if runtime is None:
        return
    from spark.connections.startup import loader

    server = _server()
    load = loader(
        sync_database(request), server=server, config_path=None if server else _config_path()
    )
    await asyncio.to_thread(runtime.refresh, load)


def _error(message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _audit(
    request: Request,
    connection_id: str,
    change: str,
    *,
    action: str = audit.SETTING_CHANGED,
    resource_type: str = "connection",
    resource_id: str | None = None,
    **details: Any,
) -> None:
    """An audit event for a connection change: names and kinds only, never values."""
    await audit.emit_request(
        request,
        action,
        resource_type=resource_type,
        resource_id=resource_id or connection_id,
        details={"connection_id": connection_id, "change": change, **details},
    )


async def _write_secrets(request: Request, connection_id: str, secrets: Mapping[str, str]) -> None:
    from spark.instance_settings.secrets import announce

    store = _secrets(request)
    for name in sorted(secrets):
        await asyncio.to_thread(store.set, name, secrets[name])
        if _server():
            await announce(database(request), name)
        await _audit(
            request,
            connection_id,
            "secret_set",
            action=audit.SECRET_CHANGED,
            resource_type="connection_secret",
            resource_id=name,
        )


async def _drop_secrets(request: Request, names: set[str]) -> None:
    """Remove konfig secrets a connection no longer refers to (only its own names)."""
    from spark.instance_settings.secrets import announce

    store = _secrets(request)
    for name in sorted(names):
        if not name.startswith("connections."):
            continue
        try:
            await asyncio.to_thread(store.delete, name)
        except Exception:  # noqa: BLE001 - an entry already gone is not an error
            continue
        if _server():
            await announce(database(request), name)


def _catalogue_provider(runtime: Any, connection: Connection) -> str:
    try:
        return str(runtime.validate(connection).catalogue_provider)
    except ConnectionInvalid:
        return connection.kind


def _residency_text(policy: ResidencyPolicy) -> str:
    scopes = ", ".join(s.replace("_", " ") for s in residency_record(policy)["scopes"]) or "none"
    if policy.regions is None:
        return scopes
    return f"{scopes} in {', '.join(sorted(policy.regions)) or 'no Region'}"


async def _stored_models(
    store: ConnectionStore, connection_id: str
) -> list[ConnectionModel] | None:
    """The connection's model rows, or None when it has none stored (uncurated)."""
    models = await store.list_models(connection_id)
    if models:
        return models
    if isinstance(store, FileConnectionStore) and await asyncio.to_thread(
        store.has_models, connection_id
    ):
        return []
    return None


async def _view(request: Request, runtime: Any, connection: Connection) -> dict[str, Any]:
    store = _store(request)
    overlays = await repo.list_overlays(database(request))
    return discovery.models_view(
        connection,
        runtime.catalogue,
        _catalogue_provider(runtime, connection),
        await _stored_models(store, connection.id),
        discovery.RESULTS.get(connection.id),
        {(o.provider, o.model_id) for o in overlays},
    )


async def _connection_or_404(request: Request, connection_id: str) -> Connection | None:
    return await _store(request).get_connection(connection_id)


def _not_found() -> JSONResponse:
    return _error("No such connection.", 404)


# -- Pages ----------------------------------------------------------------------------


@router.get(PAGE_PATH, response_class=HTMLResponse)
async def connections_page(request: Request) -> Response:
    """The administrator's Connections page (server); personal mode's live in Settings."""
    if not _server():
        return RedirectResponse(url="/settings#section-connections", status_code=303)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "admin_connections.html", {"server": True}
    )
    return response


# -- Services and the list ------------------------------------------------------------


@router.get(f"{API}/services")
async def list_services(request: Request) -> JSONResponse:
    """The add-connection drop-down: the nine services in M4 with their modal fields."""
    return JSONResponse(
        {
            "server": _server(),
            "tiers": list(TIERS),
            "scopes": ["local", "in_region", "cross_region", "global"],
            "services": [
                {
                    "key": s.key,
                    "label": s.label,
                    "kind": s.kind,
                    "residency": s.residency,
                    "fields": [f.as_dict() for f in FORMS[s.key]],
                }
                for s in SERVICES.values()
            ],
        }
    )


async def _summary(
    request: Request, runtime: Any, connection: Connection, failures: Mapping[str, str]
) -> dict[str, Any]:
    service = SERVICES.get(connection.service or "")
    view = await _view(request, runtime, connection)
    enabled_models = sum(1 for m in view["models"] if m["enabled"] and not m["refused"])
    if not connection.enabled:
        status = "disabled"
    elif connection.id in failures:
        status = "failed"
    else:
        status = "running"
    record = discovery.RESULTS.get(connection.id)
    item: dict[str, Any] = {
        "id": connection.id,
        "name": connection.name,
        "service": connection.service,
        "service_label": service.label if service is not None else connection.kind,
        "kind": connection.kind,
        "region": connection.region,
        "residency": residency_record(connection.residency),
        "residency_text": _residency_text(connection.residency),
        "enabled": connection.enabled,
        "status": status,
        "failure": failures.get(connection.id),
        "enabled_models": enabled_models,
        "discovery": record.as_dict() if record is not None else None,
    }
    if _server():
        db = database(request)
        item["metered"] = (await repo.get_metering(db, connection.id)).metered
        item["assignments"] = len(await repo.assignments(db, connection.id))
    return item


@router.get(API)
async def list_connections(request: Request) -> JSONResponse:
    """Every connection with its service, Region, residency, metering, models,
    assignments and last discovery."""
    runtime = _runtime(request)
    if runtime is None:
        return _error(NOT_STARTED, 503)
    failures = runtime.failures()
    found = await _store(request).list_connections()
    items = [await _summary(request, runtime, c, failures) for c in found]
    return JSONResponse({"server": _server(), "connections": items})


@router.get(API + "/{connection_id}")
async def get_connection(connection_id: str, request: Request) -> JSONResponse:
    """One connection as the edit modal shows it: values, and secrets as set or not set."""
    connection = await _connection_or_404(request, connection_id)
    if connection is None:
        return _not_found()
    values, secrets = describe_settings(
        connection.service or "", connection.settings, _secret_set(request)
    )
    return JSONResponse(
        {
            "id": connection.id,
            "name": connection.name,
            "service": connection.service,
            "enabled": connection.enabled,
            "values": values,
            "secrets": secrets,
            "residency": residency_record(connection.residency),
            "default_residency": residency_record(default_residency(connection.settings)),
        }
    )


# -- Create and edit ------------------------------------------------------------------


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    return slug or "connection"


async def _new_id(store: ConnectionStore, requested: Any, name: str) -> str | JSONResponse:
    taken = {c.id for c in await store.list_connections()}
    if isinstance(requested, str) and requested.strip():
        wanted = requested.strip()
        if not re.fullmatch(CONNECTION_ID_PATTERN, wanted):
            return _error(
                "A connection id is 1 to 63 lower case letters, digits and hyphens, "
                "starting with a letter or digit."
            )
        if wanted in taken:
            return _error(f"A connection with the id {wanted!r} already exists.", 409)
        return wanted
    base = _slug(name)
    candidate, n = base, 2
    while candidate in taken:
        suffix = f"-{n}"
        candidate, n = base[: 63 - len(suffix)] + suffix, n + 1
    return candidate


def _residency(data: Mapping[str, Any], settings: Mapping[str, Any]) -> ResidencyPolicy:
    chosen = data.get("residency")
    if not isinstance(chosen, Mapping) or chosen.get("default", True):
        return default_residency(settings)
    regions = chosen.get("regions")
    if isinstance(regions, str):
        regions = [r.strip() for r in regions.split(",") if r.strip()]
    return residency_from_record(
        {"scopes": list(chosen.get("scopes") or []), "regions": regions or None}
    )


def _flat(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        found: dict[str, Any] = {}
        for key, item in value.items():
            found.update(_flat(item, f"{path}.{key}" if path else str(key)))
        return found
    return {path: value}


def _changed_fields(before: Connection | None, after: Connection) -> list[str]:
    """The names of what changed (never values)."""
    old = _flat(before.settings) if before is not None else {}
    new = _flat(after.settings)
    names = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    if before is None or before.name != after.name:
        names.add("name")
    if before is None or residency_record(before.residency) != residency_record(after.residency):
        names.add("residency")
    if before is not None and before.enabled != after.enabled:
        names.add("enabled")
    return sorted(names)


async def _save(request: Request, existing: Connection | None) -> JSONResponse:
    data = await _body(request)
    store = _store(request)
    runtime = _runtime(request)
    if runtime is None:
        return _error(NOT_STARTED, 503)
    service = existing.service if existing is not None else data.get("service")
    if not isinstance(service, str) or service not in SERVICES:
        return _error("Choose one of the services listed.")
    if existing is not None and data.get("service") not in (None, service):
        return _error("A connection's service cannot change. Add a new connection instead.")
    name = str(data.get("name") or (existing.name if existing is not None else "")).strip()
    if not name:
        return _error("Give the connection a name.")
    if existing is not None:
        connection_id = existing.id
    else:
        chosen = await _new_id(store, data.get("id"), name)
        if isinstance(chosen, JSONResponse):
            return chosen
        connection_id = chosen
    values = data.get("values") if isinstance(data.get("values"), Mapping) else {}
    typed = data.get("secrets") if isinstance(data.get("secrets"), Mapping) else {}
    try:
        settings, to_write = build_settings(
            service,
            connection_id,
            values,  # type: ignore[arg-type]
            typed,  # type: ignore[arg-type]
            existing.settings if existing is not None else None,
        )
        residency = _residency(data, settings)
    except (FormRefused, ConnectionInvalid) as refused:
        return _error(str(refused))
    enabled = bool(data.get("enabled", existing.enabled if existing is not None else True))
    connection = Connection(connection_id, name, settings, residency, enabled)
    try:
        runtime.validate(connection)
    except ConnectionInvalid as refused:
        # conduit's refusal names fields and kinds, never a value.
        return _error(str(refused))
    await _write_secrets(request, connection_id, to_write)
    await store.save_connection(connection)
    if existing is not None:
        await _drop_secrets(request, _references(existing.settings) - _references(settings))
    await _audit(
        request,
        connection_id,
        "created" if existing is None else "changed",
        fields=_changed_fields(existing, connection),
    )
    await _refresh(request)
    return JSONResponse(
        {"id": connection_id, "saved": True}, status_code=201 if not existing else 200
    )


@router.post(API)
async def create_connection(request: Request) -> JSONResponse:
    """Add a connection: the modal's values, validated through conduit's registry."""
    return await _save(request, None)


@router.put(API + "/{connection_id}")
async def update_connection(connection_id: str, request: Request) -> JSONResponse:
    """Edit a connection with the same modal (a blank secret keeps the one stored)."""
    existing = await _connection_or_404(request, connection_id)
    if existing is None:
        return _not_found()
    return await _save(request, existing)


async def _replace(request: Request, before: Connection, after: Connection, change: str) -> None:
    await _store(request).save_connection(after)
    await _audit(request, after.id, change, fields=_changed_fields(before, after))
    await _refresh(request)


@router.post(API + "/{connection_id}/enabled")
async def set_enabled(connection_id: str, request: Request) -> JSONResponse:
    """Enable or disable a connection."""
    existing = await _connection_or_404(request, connection_id)
    if existing is None:
        return _not_found()
    enabled = bool((await _body(request)).get("enabled"))
    after = Connection(existing.id, existing.name, existing.settings, existing.residency, enabled)
    await _replace(request, existing, after, "enabled" if enabled else "disabled")
    return JSONResponse({"id": connection_id, "enabled": enabled})


@router.post(API + "/{connection_id}/rename")
async def rename_connection(connection_id: str, request: Request) -> JSONResponse:
    """Rename a connection (its id, and every chosen model naming it, stay)."""
    existing = await _connection_or_404(request, connection_id)
    if existing is None:
        return _not_found()
    name = str((await _body(request)).get("name") or "").strip()
    if not name or len(name) > 128:
        return _error("A connection name is 1 to 128 characters.")
    after = Connection(existing.id, name, existing.settings, existing.residency, existing.enabled)
    await _replace(request, existing, after, "renamed")
    return JSONResponse({"id": connection_id, "name": name})


async def _actions_using(request: Request, connection_id: str) -> list[dict[str, Any]]:
    """The scheduled actions whose model is on the connection (every user's)."""
    runtime = _runtime(request)
    rows = await autonomous_actions.action_models_all_users(database(request))
    using: list[dict[str, Any]] = []
    for row in rows:
        model = str(row["model_id"] or "")
        ref = ModelRef.parse(model)
        if ref is None and model and runtime is not None:
            ref = runtime.resolve(model)
        if ref is not None and ref.connection_id == connection_id:
            using.append(
                {"id": int(row["id"]), "name": str(row["name"]), "enabled": bool(row["is_enabled"])}
            )
    return using


@router.delete(API + "/{connection_id}")
async def delete_connection(connection_id: str, request: Request) -> JSONResponse:
    """Delete a connection, its models, metering, assignments and secrets; refused (409)
    while a scheduled action uses it, naming the actions."""
    existing = await _connection_or_404(request, connection_id)
    if existing is None:
        return _not_found()
    using = await _actions_using(request, connection_id)
    if using:
        names = ", ".join(a["name"] for a in using)
        return _error(
            f"Scheduled actions use this connection: {names}. "
            "Change their model or delete them first.",
            409,
            actions=using,
        )
    await _store(request).delete_connection(connection_id)
    await _drop_secrets(request, _references(existing.settings))
    discovery.RESULTS.forget(connection_id)
    await _audit(request, connection_id, "deleted")
    await _refresh(request)
    return JSONResponse({"id": connection_id, "deleted": True})


# -- Models ---------------------------------------------------------------------------


@router.get(API + "/{connection_id}/models")
async def list_models(connection_id: str, request: Request) -> JSONResponse:
    """The connection's models view (see :func:`spark.connections.discovery.models_view`)."""
    runtime = _runtime(request)
    if runtime is None:
        return _error(NOT_STARTED, 503)
    connection = await _connection_or_404(request, connection_id)
    if connection is None:
        return _not_found()
    return JSONResponse({"id": connection_id, **await _view(request, runtime, connection)})


@router.post(API + "/{connection_id}/discover")
async def discover_models(connection_id: str, request: Request) -> JSONResponse:
    """Ask conduit now what the connection can reach, then answer its models view."""
    runtime = _runtime(request)
    if runtime is None:
        return _error(NOT_STARTED, 503)
    connection = await _connection_or_404(request, connection_id)
    if connection is None:
        return _not_found()
    await asyncio.to_thread(discovery.discover, runtime, connection)
    return JSONResponse({"id": connection_id, **await _view(request, runtime, connection)})


@router.put(API + "/{connection_id}/models")
async def save_model(connection_id: str, request: Request) -> JSONResponse:
    """Enable or disable a model on the connection, or set its tier."""
    runtime = _runtime(request)
    if runtime is None:
        return _error(NOT_STARTED, 503)
    connection = await _connection_or_404(request, connection_id)
    if connection is None:
        return _not_found()
    data = await _body(request)
    model_id = str(data.get("model_id") or "")
    view = await _view(request, runtime, connection)
    rows = {m["model_id"]: m for m in view["models"]}
    if model_id not in rows:
        return _error("That model is not in the catalogue for this connection.", 404)
    current = rows[model_id]
    tier = data.get("tier", current["tier"])
    if tier not in TIERS:
        return _error("A tier is lower, standard or higher.")
    enabled = bool(data.get("enabled", current["enabled"]))
    store = _store(request)
    if not view["curated"]:
        # The first choice curates the connection: keep every model it offered so far.
        for row in view["models"]:
            if row["model_id"] != model_id:
                await store.save_model(ConnectionModel(connection_id, row["model_id"]))
    await store.save_model(ConnectionModel(connection_id, model_id, enabled, tier))
    await _audit(request, connection_id, "model", model_id=model_id, enabled=enabled, tier=tier)
    await _refresh(request)
    return JSONResponse({"id": connection_id, **await _view(request, runtime, connection)})


_OVERLAY_SCOPES: Final = frozenset({"local", "in_region", "cross_region", "global"})


def _int(value: Any, label: str, *, optional: bool = False) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise FormRefused(f"{label} is required")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise FormRefused(f"{label} must be a whole number") from None
    if number < 1:
        raise FormRefused(f"{label} must be at least 1")
    return number


def _overlay_row(provider: str, data: Mapping[str, Any]) -> repo.OverlayRow:
    model_id = str(data.get("model_id") or "").strip()
    if not model_id or len(model_id) > 256:
        raise FormRefused("Give the model id, as the service names it")
    kind = data.get("kind", "chat")
    if kind not in ("chat", "embedding"):
        raise FormRefused("A model is a chat or an embedding model")
    scope = data.get("residency_scope")
    if scope not in _OVERLAY_SCOPES:
        raise FormRefused("Choose where the model processes requests")
    regions = data.get("regions") or []
    if isinstance(regions, str):
        regions = [r.strip() for r in regions.split(",") if r.strip()]
    context = _int(data.get("context_window"), "Context window")
    assert context is not None
    return repo.OverlayRow(
        provider=provider,
        model_id=model_id,
        kind="embedding" if kind == "embedding" else "chat",
        context_window=context,
        output_limit=_int(data.get("output_limit"), "Output limit", optional=kind != "chat"),
        embedding_dimensions=_int(
            data.get("embedding_dimensions"), "Embedding dimensions", optional=kind == "chat"
        ),
        tools=bool(data.get("tools")),
        vision=bool(data.get("vision")),
        prompt_caching=bool(data.get("prompt_caching")),
        structured_output=bool(data.get("structured_output")),
        residency_scope=str(scope),
        regions=tuple(sorted({str(r) for r in regions})),
    )


async def _shipped(request: Request, runtime: Any, row: repo.OverlayRow) -> bool:
    """Whether conduit's catalogue already ships ``row``'s model for its provider.

    An overlay row on a shipped key would override that entry for every connection of
    the service, so a declaration may add a model or correct one of SPARK's own rows,
    never replace conduit's.
    """
    if runtime.catalogue.get(row.provider, row.model_id) is None:
        return False
    own = {(o.provider, o.model_id) for o in await repo.list_overlays(database(request))}
    return (row.provider, row.model_id) not in own


@router.post(API + "/{connection_id}/overlays")
async def declare_model(connection_id: str, request: Request) -> JSONResponse:
    """Declare a model the catalogue lacks, as a SPARK overlay row for the connection's
    catalogue provider (validated as a conduit catalogue entry).

    The model id may be one discovery listed or one typed in (a Foundry deployment
    name, which discovery cannot list). An id conduit's catalogue already ships for
    the provider is refused (409); one of SPARK's own rows is replaced."""
    runtime = _runtime(request)
    if runtime is None:
        return _error(NOT_STARTED, 503)
    connection = await _connection_or_404(request, connection_id)
    if connection is None:
        return _not_found()
    provider = _catalogue_provider(runtime, connection)
    try:
        row = _overlay_row(provider, await _body(request))
        entry_of(row)
    except FormRefused as refused:
        return _error(str(refused))
    except ValueError as refused:
        problems = getattr(refused, "errors", None)
        detail = (
            "; ".join(str(e.get("msg", "")) for e in problems())
            if callable(problems)
            else "not a valid catalogue entry"
        )
        return _error(f"conduit refused the entry: {detail}")
    if await _shipped(request, runtime, row):
        return _error(
            f"The catalogue already lists {row.model_id} for this service. Give the model "
            "another id (for a Foundry deployment, its own deployment name).",
            409,
        )
    await repo.save_overlay(database(request), row)
    await _audit(request, connection_id, "overlay_declared", model_id=row.model_id)
    await _refresh(request)
    return JSONResponse({"id": connection_id, **await _view(request, runtime, connection)})


# -- Metering (server mode) -----------------------------------------------------------


_METERING_FIELDS: Final = (
    "metered",
    "base_rate_tokens",
    "higher_multiplier",
    "lower_multiplier",
    "input_weight",
    "output_weight",
    "cache_read_weight",
    "cache_write_weight",
)


def _metering_dict(metering: repo.Metering) -> dict[str, Any]:
    from spark.usage.credits import estimate

    defaults = repo.Metering(metering.connection_id)
    return {
        "id": metering.connection_id,
        **{name: getattr(metering, name) for name in _METERING_FIELDS},
        "defaults": {name: getattr(defaults, name) for name in _METERING_FIELDS[1:]},
        "sample": dict(SAMPLE_TURN),
        "example": estimate(metering, SAMPLE_TURN).as_dict(),
    }


@router.get(API + "/{connection_id}/metering")
async def get_metering(connection_id: str, request: Request) -> JSONResponse:
    """The connection's metering and a worked example (credit-metering's estimate)."""
    if not _server():
        return _error("Personal mode meters no credits.", 404)
    if await _connection_or_404(request, connection_id) is None:
        return _not_found()
    return JSONResponse(_metering_dict(await repo.get_metering(database(request), connection_id)))


@router.put(API + "/{connection_id}/metering")
async def save_metering(connection_id: str, request: Request) -> JSONResponse:
    """Set the connection's metering: metered, base rate, tier multipliers, token weights."""
    if not _server():
        return _error("Personal mode meters no credits.", 404)
    if await _connection_or_404(request, connection_id) is None:
        return _not_found()
    db = database(request)
    before = await repo.get_metering(db, connection_id)
    data = await _body(request)
    try:
        after = repo.Metering(
            connection_id=connection_id,
            metered=bool(data.get("metered", before.metered)),
            base_rate_tokens=int(data.get("base_rate_tokens", before.base_rate_tokens)),
            **{name: float(data.get(name, getattr(before, name))) for name in _METERING_FIELDS[2:]},
        )
        await repo.save_metering(db, after)
    except (TypeError, ValueError) as refused:
        return _error(str(refused) or "Give numbers for the rates and weights.")
    changed = [n for n in _METERING_FIELDS if getattr(before, n) != getattr(after, n)]
    await _audit(request, connection_id, "metering", fields=changed)
    await _refresh(request)
    return JSONResponse(_metering_dict(after))


# -- Assignments (server mode) --------------------------------------------------------


async def _people(request: Request) -> dict[str, str]:
    """Every SPARK user linked to a warden user, by guid, named by email address."""
    from spark.database import usage
    from spark.web.endpoints.identity import identity_runtime

    users_api = identity_runtime(request).identity.users
    emails: dict[str, str] = {}
    after: str | None = None
    while True:
        page = await users_api.list(limit=200, after=after)
        emails.update({str(u.id): u.email for u in page})
        if len(page) < 200:
            break
        after = users_api.cursor(page[-1])
    linked = await usage.warden_users_all_users(database(request))
    return {guid: emails.get(warden_id, guid) for guid, warden_id in linked.items()}


async def _assignments_payload(request: Request, connection_id: str) -> dict[str, Any]:
    db = database(request)
    people = await _people(request)
    metered = (await repo.get_metering(db, connection_id)).metered
    items = [
        {
            "role": a.role,
            "user_guid": a.user_guid,
            "user": people.get(a.user_guid, a.user_guid) if a.user_guid else None,
            "monthly_credits": a.monthly_credits,
            "no_limit": a.monthly_credits is None,
        }
        for a in await repo.assignments(db, connection_id)
    ]
    return {
        "id": connection_id,
        "metered": metered,
        "assignments": items,
        "roles": sorted(ROLES),
        "users": [
            {"user_guid": guid, "label": label}
            for guid, label in sorted(people.items(), key=lambda p: p[1].lower())
        ],
    }


@router.get(API + "/{connection_id}/assignments")
async def list_assignments(connection_id: str, request: Request) -> JSONResponse:
    """The connection's role and user assignments with their monthly allowances."""
    if not _server():
        return _error("Personal mode has no assignments.", 404)
    if await _connection_or_404(request, connection_id) is None:
        return _not_found()
    return JSONResponse(await _assignments_payload(request, connection_id))


def _allowance(data: Mapping[str, Any], metered: bool) -> int | None:
    """The monthly allowance asked for: a whole number of credits, or None for no limit.

    On a metered connection a blank allowance is refused unless "No limit" is chosen
    explicitly (an unset allowance means no limit, decision 88).
    """
    raw = data.get("monthly_credits")
    blank = raw is None or (isinstance(raw, str) and not raw.strip())
    no_limit = bool(data.get("no_limit"))
    if no_limit and not blank:
        raise FormRefused("Give a monthly allowance or choose No limit, not both")
    if blank:
        if metered and not no_limit:
            raise FormRefused(
                "This connection is metered: give a monthly allowance in credits, "
                "or choose No limit"
            )
        return None
    try:
        credits = int(str(raw).strip())
    except ValueError:
        raise FormRefused("A monthly allowance is a whole number of credits") from None
    if credits < 0:
        raise FormRefused("A monthly allowance cannot be negative")
    return credits


@router.put(API + "/{connection_id}/assignments")
async def save_assignment(connection_id: str, request: Request) -> JSONResponse:
    """Assign the connection to a role or a user, with a monthly allowance when metered."""
    if not _server():
        return _error("Personal mode has no assignments.", 404)
    if await _connection_or_404(request, connection_id) is None:
        return _not_found()
    data = await _body(request)
    db = database(request)
    role = data.get("role") or None
    user_guid = data.get("user_guid") or None
    if (role is None) == (user_guid is None):
        return _error("Assign the connection to a role or to a user.")
    if role is not None and role not in ROLES:
        return _error("Choose one of the roles listed.")
    if user_guid is not None and str(user_guid) not in await _people(request):
        return _error("Choose one of the users listed.")
    try:
        credits = _allowance(data, (await repo.get_metering(db, connection_id)).metered)
    except FormRefused as refused:
        return _error(str(refused))
    await repo.save_assignment(
        db,
        repo.Assignment(
            connection_id,
            role=str(role) if role is not None else None,
            user_guid=str(user_guid) if user_guid is not None else None,
            monthly_credits=credits,
        ),
    )
    await _audit(
        request,
        connection_id,
        "assignment",
        role=role,
        assignee=user_guid,
        allowance=credits,
        no_limit=credits is None,
    )
    await _refresh(request)
    return JSONResponse(await _assignments_payload(request, connection_id))


@router.delete(API + "/{connection_id}/assignments")
async def delete_assignment(connection_id: str, request: Request) -> JSONResponse:
    """Remove one role's or one user's assignment (``?role=`` or ``?user_guid=``)."""
    if not _server():
        return _error("Personal mode has no assignments.", 404)
    role = request.query_params.get("role") or None
    user_guid = request.query_params.get("user_guid") or None
    if (role is None) == (user_guid is None):
        return _error("Name a role or a user.")
    removed = await repo.delete_assignment(
        database(request), connection_id, role=role, user_guid=user_guid
    )
    if not removed:
        return _error("No such assignment.", 404)
    await _audit(request, connection_id, "unassigned", role=role, assignee=user_guid)
    await _refresh(request)
    return JSONResponse(await _assignments_payload(request, connection_id))
