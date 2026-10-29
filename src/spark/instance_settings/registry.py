"""The typed registry of every setting SPARK accepts: key, scope, default and validation.

Each key has a :class:`Scope`:

* ``INSTANCE``: administrator controlled. Postgres (``instance_settings``) in server
  mode, ``config.yaml`` in personal mode.
* ``USER``: a per-user preference (A4). ``user_preferences`` in server mode,
  ``config.yaml`` in personal mode.
* ``STARTUP``: read once from konfig at start-up (database URL, public base URL,
  warden configuration, logging). Never stored in the database.

A key flagged ``secret`` is only ever held by konfig's secrets store, under
:func:`secret_name` (the dotted key with its group, ``database.url`` becomes
``database_url``). Validation errors name the key and never echo the value.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "InvalidSettingError",
    "Kind",
    "Scope",
    "SettingSpec",
    "SettingsError",
    "UnknownSettingError",
    "coerce",
    "is_known",
    "keys",
    "legacy_secret_name",
    "secret_keys",
    "secret_name",
    "spec",
]


class SettingsError(ValueError):
    """A setting was refused."""


class UnknownSettingError(SettingsError):
    """The key is not in the registry."""

    def __init__(self, key: str) -> None:
        super().__init__(f"unknown setting {key!r}")
        self.key = key


class InvalidSettingError(SettingsError):
    """The value is not valid for the key (the message never contains the value)."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"invalid value for {key}: {reason}")
        self.key = key


class Scope(StrEnum):
    INSTANCE = "instance"
    USER = "user"
    STARTUP = "startup"


class Kind(StrEnum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    URL = "url"
    CHOICE = "choice"
    STR_LIST = "str_list"
    MCP_SERVERS = "mcp_servers"
    POLICY_ACTIONS = "policy_actions"


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: Kind
    scope: Scope
    default: Any = None
    secret: bool = False
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})
_MCP_TRANSPORTS = ("stdio", "http", "sse")
_POLICY_ACTIONS = ("mark", "warn", "withhold")


def _bool(spec: SettingSpec, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise InvalidSettingError(spec.key, "expected true or false")


def _bounded(spec: SettingSpec, number: float) -> None:
    if spec.minimum is not None and number < spec.minimum:
        raise InvalidSettingError(spec.key, f"must be at least {spec.minimum:g}")
    if spec.maximum is not None and number > spec.maximum:
        raise InvalidSettingError(spec.key, f"must be at most {spec.maximum:g}")


def _int(spec: SettingSpec, value: Any) -> int:
    number: int
    if isinstance(value, bool):
        raise InvalidSettingError(spec.key, "expected a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value.strip())
    else:
        raise InvalidSettingError(spec.key, "expected a whole number")
    _bounded(spec, number)
    return number


def _float(spec: SettingSpec, value: Any) -> float:
    if isinstance(value, bool):
        raise InvalidSettingError(spec.key, "expected a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidSettingError(spec.key, "expected a number") from None
    if not math.isfinite(number):
        raise InvalidSettingError(spec.key, "expected a finite number")
    _bounded(spec, number)
    return number


def _str(spec: SettingSpec, value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidSettingError(spec.key, "expected text")
    return value


def _url(spec: SettingSpec, value: Any) -> str:
    text = _str(spec, value).strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise InvalidSettingError(spec.key, "expected an http or https URL")
    return text


def _choice(spec: SettingSpec, value: Any) -> str:
    if not isinstance(value, str) or value not in spec.choices:
        raise InvalidSettingError(spec.key, f"expected one of {', '.join(spec.choices)}")
    return value


def _str_list(spec: SettingSpec, value: Any) -> list[str]:
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise InvalidSettingError(spec.key, "expected a list of text")
    if not all(isinstance(item, str) for item in items):
        raise InvalidSettingError(spec.key, "expected a list of text")
    return [item.strip() for item in items if item.strip()]


def _mcp_servers(spec: SettingSpec, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise InvalidSettingError(spec.key, "expected a list of servers")
    names: set[str] = set()
    servers: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise InvalidSettingError(spec.key, "each server must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidSettingError(spec.key, "each server needs a name")
        if name in names:
            raise InvalidSettingError(spec.key, f"server name {name!r} is repeated")
        if entry.get("transport") not in _MCP_TRANSPORTS:
            raise InvalidSettingError(
                spec.key, f"server {name!r} transport must be one of {', '.join(_MCP_TRANSPORTS)}"
            )
        names.add(name)
        servers.append(dict(entry))
    return servers


def _policy_actions(spec: SettingSpec, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InvalidSettingError(spec.key, "expected a mapping of source category to action")
    actions: dict[str, str] = {}
    for category, action in value.items():
        if not isinstance(category, str) or not category.strip():
            raise InvalidSettingError(spec.key, "each source category needs a name")
        if action not in _POLICY_ACTIONS:
            raise InvalidSettingError(
                spec.key, f"action for {category!r} must be one of {', '.join(_POLICY_ACTIONS)}"
            )
        actions[category] = action
    return actions


_COERCERS: dict[Kind, Callable[[SettingSpec, Any], Any]] = {
    Kind.BOOL: _bool,
    Kind.INT: _int,
    Kind.FLOAT: _float,
    Kind.STR: _str,
    Kind.URL: _url,
    Kind.CHOICE: _choice,
    Kind.STR_LIST: _str_list,
    Kind.MCP_SERVERS: _mcp_servers,
    Kind.POLICY_ACTIONS: _policy_actions,
}


# -- The registry ---------------------------------------------------------------

_I, _U, _S = Scope.INSTANCE, Scope.USER, Scope.STARTUP


def _toggle(key: str, scope: Scope = _I, default: bool = False) -> SettingSpec:
    return SettingSpec(key, Kind.BOOL, scope, default)


def _int_spec(
    key: str,
    default: int,
    minimum: float | None = 0,
    maximum: float | None = None,
    scope: Scope = _I,
) -> SettingSpec:
    return SettingSpec(key, Kind.INT, scope, default, minimum=minimum, maximum=maximum)


def _ratio(key: str, default: float) -> SettingSpec:
    return SettingSpec(key, Kind.FLOAT, _I, default, minimum=0.0, maximum=1.0)


def _text(key: str, default: str = "", scope: Scope = _I) -> SettingSpec:
    return SettingSpec(key, Kind.STR, scope, default)


def _pick(key: str, choices: Iterable[str], default: str, scope: Scope = _I) -> SettingSpec:
    return SettingSpec(key, Kind.CHOICE, scope, default, choices=tuple(choices))


def _secret(key: str, scope: Scope = _I) -> SettingSpec:
    return SettingSpec(key, Kind.STR, scope, "", secret=True)


_SPECS: tuple[SettingSpec, ...] = (
    # Start-up configuration (konfig only)
    _pick("mode", ("personal", "server"), "personal", _S),
    _secret("database.url", _S),
    SettingSpec("server.public_base_url", Kind.URL, _S, ""),
    SettingSpec("warden.url", Kind.URL, _S, "", description="Placeholder; wave 3."),
    _text("warden.client_id", "", _S),
    _secret("warden.client_secret", _S),
    _toggle("interface.ssl.enabled", _S),
    _int_spec("interface.session_timeout_minutes", 60, 1, None, _S),
    _pick("logging.level", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), "INFO", _S),
    _pick("logging.format", ("text", "json"), "text", _S),
    _int_spec("logging.retention_runs", 10, 1, None, _S),
    _toggle("daemon.enabled", _S),
    _int_spec("daemon.heartbeat_interval", 30, 1, None, _S),
    # Providers and models offered (administrator)
    _toggle("providers.anthropic.enabled"),
    _secret("providers.anthropic.api_key"),
    _toggle("providers.aws_bedrock.enabled"),
    _text("providers.aws_bedrock.region", "us-east-1"),
    _pick("providers.aws_bedrock.auth_method", ("sso", "iam", "session"), "sso"),
    _text("providers.aws_bedrock.profile"),
    _secret("providers.aws_bedrock.access_key"),
    _secret("providers.aws_bedrock.secret_key"),
    _secret("providers.aws_bedrock.session_token"),
    _int_spec("providers.aws_bedrock.read_timeout", 300, 1, 3600),
    _toggle("providers.ollama.enabled"),
    SettingSpec("providers.ollama.base_url", Kind.URL, _I, "http://localhost:11434"),
    _toggle("providers.google_gemini.enabled"),
    _secret("providers.google_gemini.api_key"),
    _toggle("providers.xai.enabled"),
    _secret("providers.xai.api_key"),
    SettingSpec(
        "models.offered",
        Kind.STR_LIST,
        _I,
        [],
        description="Model ids offered to users; empty offers every model.",
    ),
    _pick("default_model.mode", ("default", "mandatory"), "default"),
    # MCP servers and tool policy (administrator)
    SettingSpec("mcp.servers", Kind.MCP_SERVERS, _I, []),
    SettingSpec(
        "tool_policy.actions",
        Kind.POLICY_ACTIONS,
        _I,
        {},
        description="Untrusted-content action per source category (A16).",
    ),
    _toggle("tool_permissions.auto_approve"),
    _toggle("prompt_inspection.enabled"),
    _pick("prompt_inspection.level", ("basic", "standard", "strict"), "standard"),
    _pick("prompt_inspection.action", ("block", "warn", "sanitize", "log_only"), "warn"),
    # Conversation behaviour (administrator)
    _ratio("conversation.rollup_threshold", 0.3),
    _ratio("conversation.rollup_summary_ratio", 0.3),
    _ratio("conversation.emergency_rollup_threshold", 0.95),
    _toggle("conversation.prompt_caching", default=True),
    _int_spec("conversation.max_tool_iterations", 25, 1),
    _int_spec("conversation.max_tool_selections", 30, 1),
    _int_spec("conversation.max_tool_result_tokens", 4000, 1),
    # Agents and actions (administrator)
    _toggle("embedded_tools.agents.enabled"),
    _pick("embedded_tools.agents.default_mode", ("orchestrator", "chain"), "orchestrator"),
    _pick(
        "embedded_tools.agents.model_selection",
        ("same", "auto_select", "auto_select_approved"),
        "same",
    ),
    _int_spec("embedded_tools.agents.max_concurrent", 5, 1),
    _int_spec("embedded_tools.agents.max_iterations", 15, 1),
    _toggle("autonomous_actions.enabled"),
    _int_spec("autonomous_actions.max_concurrent", 3, 1),
    # Embedded tools (administrator)
    _toggle("embedded_tools.filesystem.enabled", default=True),
    _pick("embedded_tools.filesystem.mode", ("read", "read_write"), "read"),
    SettingSpec("embedded_tools.filesystem.allowed_paths", Kind.STR_LIST, _I, []),
    _toggle("embedded_tools.documents.enabled", default=True),
    _pick("embedded_tools.documents.mode", ("read", "read_write"), "read"),
    _int_spec("embedded_tools.documents.max_file_size_mb", 50, 1),
    _toggle("embedded_tools.archives.enabled", default=True),
    _pick("embedded_tools.archives.mode", ("list", "extract"), "list"),
    _toggle("embedded_tools.web.enabled", default=True),
    _pick(
        "embedded_tools.web.search_engine",
        ("duckduckgo", "brave", "google", "bing", "searxng"),
        "duckduckgo",
    ),
    _secret("embedded_tools.web.brave_api_key"),
    _secret("embedded_tools.web.google_api_key"),
    _secret("embedded_tools.web.bing_api_key"),
    SettingSpec("embedded_tools.web.searxng_url", Kind.URL, _I, ""),
    _toggle("embedded_tools.system_commands.enabled"),
    _int_spec("embedded_tools.system_commands.timeout", 30, 1, 300),
    _int_spec("embedded_tools.system_commands.max_output_chars", 50000, 1),
    SettingSpec("embedded_tools.system_commands.blocked_commands", Kind.STR_LIST, _I, []),
    _toggle("embedded_tools.system_commands.require_approval", default=True),
    # Email integration (administrator)
    _toggle("embedded_tools.email.enabled"),
    _text("embedded_tools.email.host"),
    _int_spec("embedded_tools.email.port", 587, 1, 65535),
    _text("embedded_tools.email.username"),
    _secret("embedded_tools.email.password"),
    _text("embedded_tools.email.sender"),
    _toggle("embedded_tools.email.use_tls", default=True),
    _int_spec("embedded_tools.email.max_attachment_mb", 25, 1),
    _toggle("embedded_tools.email.require_approval", default=True),
    # ElevenLabs integration (administrator)
    _pick("voice.engine", ("browser", "elevenlabs"), "browser"),
    _secret("voice.elevenlabs.api_key"),
    _pick(
        "voice.elevenlabs.model_id",
        ("eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_v3"),
        "eleven_flash_v2_5",
    ),
    _int_spec("voice.elevenlabs.monthly_character_cap", 100000, 0),
    _toggle("voice.elevenlabs.cache_enabled", default=True),
    _int_spec("voice.elevenlabs.cache_max_mb", 200, 0),
    # Files (administrator; consumed by the file areas workstream)
    _int_spec("files.quota_mb", 1024, 0),
    _int_spec("files.max_upload_mb", 50, 1),
    # Per-user preferences (A4)
    _text("default_model.model_id", "", _U),
    _text("conversation.global_instructions", "", _U),
    _pick("interface.theme", ("dark", "light"), "dark", _U),
    _text("voice.elevenlabs.default_voice_id", "", _U),
    _pick(
        "voice.interaction_mode",
        ("listen_along", "immersive", "listen_only"),
        "listen_along",
        _U,
    ),
)

_REGISTRY: dict[str, SettingSpec] = {s.key: s for s in _SPECS}
if len(_REGISTRY) != len(_SPECS):  # pragma: no cover - a programming error
    raise RuntimeError("a setting key is registered twice")


def spec(key: str) -> SettingSpec:
    """The spec for ``key``; :class:`UnknownSettingError` if it is not registered."""
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownSettingError(key) from None


def is_known(key: str) -> bool:
    return key in _REGISTRY


def keys(scope: Scope | None = None) -> list[str]:
    """Every registered key, or those of one scope, in registry order."""
    return [s.key for s in _SPECS if scope is None or s.scope is scope]


def secret_keys() -> list[str]:
    return [s.key for s in _SPECS if s.secret]


def coerce(key: str, value: Any) -> Any:
    """Validate ``value`` for ``key`` and return it in its stored form.

    Accepts the forms the settings page sends (``"true"``, numeric strings,
    comma-separated lists). Raises :class:`UnknownSettingError` or
    :class:`InvalidSettingError`.
    """
    s = spec(key)
    if s.secret:
        if not isinstance(value, str):
            raise InvalidSettingError(key, "expected text")
        return value
    return _COERCERS[s.kind](s, value)


def secret_name(key: str) -> str:
    """The secrets-store name for a secret key: the dotted key with its group, ``.`` as ``_``."""
    if not spec(key).secret:
        raise InvalidSettingError(key, "is not a secret")
    return key.replace(".", "_")


def legacy_secret_name(key: str) -> str:
    """The name SPARK 0.2 used: the dotted key without its group (``database.url`` was ``url``)."""
    if not spec(key).secret:
        raise InvalidSettingError(key, "is not a secret")
    return "_".join(key.split(".")[1:])
