"""Named model connections (M3, M4, M15).

A connection is one configured service: a name an administrator (or the personal
user) chose, the conduit provider settings for its kind, a Region where the kind
has one, and a residency policy. Several connections of one kind may exist side
by side, each with its own settings and credentials.

Credentials are never held here: every credential field is a ``secret://``
reference, resolved through konfig only when conduit builds the provider (A9).
:func:`validate` holds a connection to conduit's own settings model for its kind,
through conduit's provider registry, which refuses a plain-text credential by
field name and never echoes a value.

The nine services M4 lists map onto conduit's provider kinds:

=====================  ===================================  ==========================
Service                Shown as                             conduit kind and settings
=====================  ===================================  ==========================
``bedrock``            Amazon Bedrock                       ``bedrock``
``bedrock_mantle``     Claude through Bedrock (mantle)      ``bedrock_mantle``
``anthropic``          Anthropic                            ``anthropic``, direct
``anthropic_foundry``  Claude in Microsoft Foundry          ``anthropic``, foundry
``openai``             OpenAI                               ``openai_compat``, openai
``xai``                X.AI                                 ``openai_compat``, xai
``openai_foundry``     Microsoft Foundry (OpenAI)           ``openai_compat``, foundry
``gemini``             Google Gemini                        ``gemini``
``ollama``             Ollama                               ``ollama``
=====================  ===================================  ==========================

Residency defaults (M15): in-Region for the Bedrock, Bedrock mantle and Microsoft
Foundry services, global for the direct APIs, local for Ollama.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from conduit import ConduitError, ProviderSettings, ResidencyPolicy, ResidencyScope

__all__ = [
    "CONNECTION_ID_PATTERN",
    "SERVICES",
    "TIERS",
    "Connection",
    "ConnectionInvalid",
    "ConnectionModel",
    "Service",
    "SettingsValidator",
    "Tier",
    "default_residency",
    "residency_from_record",
    "residency_record",
    "service_of",
    "validate",
]

#: A connection id: lower case letters, digits and hyphens, starting with a letter or
#: digit. It names the conduit provider, so it never holds a separator of a model
#: reference (:mod:`spark.connections.refs`).
CONNECTION_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
_NAME_MAX: Final = 128

Tier = Literal["lower", "standard", "higher"]
#: A model's credit tier on a connection (M7); ``standard`` unless an administrator says.
TIERS: Final[tuple[Tier, ...]] = ("lower", "standard", "higher")


class ConnectionInvalid(ValueError):
    """A connection refused before it is stored or built. The message names fields only."""


@dataclass(frozen=True, slots=True)
class Service:
    """One service the add-connection list offers (M4)."""

    key: str
    label: str
    kind: str
    #: Settings every connection of this service carries (its endpoint or service name).
    fixed: Mapping[str, Any] = field(default_factory=dict)
    #: The residency scope connections of this service default to.
    residency: Literal["in_region", "global", "local"] = "global"


SERVICES: Final[Mapping[str, Service]] = {
    s.key: s
    for s in (
        Service("bedrock", "Amazon Bedrock", "bedrock", residency="in_region"),
        Service(
            "bedrock_mantle",
            "Claude through Amazon Bedrock (mantle)",
            "bedrock_mantle",
            residency="in_region",
        ),
        Service("anthropic", "Anthropic", "anthropic", {"endpoint": "direct"}),
        Service(
            "anthropic_foundry",
            "Claude in Microsoft Foundry",
            "anthropic",
            {"endpoint": "foundry"},
            residency="in_region",
        ),
        Service("openai", "OpenAI", "openai_compat", {"service": {"name": "openai"}}),
        Service("xai", "X.AI", "openai_compat", {"service": {"name": "xai"}}),
        Service(
            "openai_foundry",
            "Microsoft Foundry (OpenAI-compatible)",
            "openai_compat",
            {"service": {"name": "foundry"}},
            residency="in_region",
        ),
        Service("gemini", "Google Gemini", "gemini"),
        Service("ollama", "Ollama", "ollama", residency="local"),
    )
}


def service_of(settings: Mapping[str, Any]) -> str | None:
    """The M4 service a connection's settings describe (None for any other kind, such as
    the test stub)."""
    kind = settings.get("kind")
    if kind == "anthropic":
        return "anthropic_foundry" if settings.get("endpoint") == "foundry" else "anthropic"
    if kind == "openai_compat":
        service = settings.get("service")
        name = service.get("name") if isinstance(service, Mapping) else None
        return {"openai": "openai", "xai": "xai", "foundry": "openai_foundry"}.get(str(name))
    return kind if isinstance(kind, str) and kind in SERVICES else None


def _region(settings: Mapping[str, Any]) -> str | None:
    region = settings.get("region")
    return region if isinstance(region, str) and region else None


def default_residency(settings: Mapping[str, Any]) -> ResidencyPolicy:
    """The residency policy a new connection starts with (M15).

    In-Region services allow local and in-Region processing in the connection's own
    Region; direct APIs allow global processing; Ollama allows local processing only.
    """
    service = SERVICES.get(service_of(settings) or "")
    scope = service.residency if service is not None else "local"
    if scope == "in_region":
        region = _region(settings)
        return ResidencyPolicy(
            scopes=frozenset({ResidencyScope.LOCAL, ResidencyScope.IN_REGION}),
            regions=frozenset({region}) if region else None,
        )
    if scope == "local":
        return ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL}))
    return ResidencyPolicy()


def residency_record(policy: ResidencyPolicy) -> dict[str, Any]:
    """A residency policy as stored: sorted scopes, and sorted regions or None."""
    return {
        "scopes": sorted(scope.value for scope in policy.scopes),
        "regions": None if policy.regions is None else sorted(policy.regions),
    }


def residency_from_record(record: Any) -> ResidencyPolicy:
    """A stored residency policy (a mapping, or its JSON text). Raises ConnectionInvalid."""
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except ValueError:
            raise ConnectionInvalid("residency is not valid JSON") from None
    if not isinstance(record, Mapping):
        raise ConnectionInvalid("residency must be a mapping of scopes and regions")
    try:
        return ResidencyPolicy.model_validate(dict(record))
    except ValueError:
        raise ConnectionInvalid("residency has an unknown scope or a bad region list") from None


@dataclass(frozen=True, slots=True)
class Connection:
    """One named connection: its conduit settings and residency policy.

    ``settings`` is the conduit provider settings mapping for the connection's kind,
    ``kind`` included, with every credential a ``secret://`` reference.
    """

    id: str
    name: str
    settings: Mapping[str, Any]
    residency: ResidencyPolicy = field(default_factory=ResidencyPolicy)
    enabled: bool = True

    @property
    def kind(self) -> str:
        return str(self.settings.get("kind", ""))

    @property
    def region(self) -> str | None:
        return _region(self.settings)

    @property
    def service(self) -> str | None:
        return service_of(self.settings)

    def fingerprint(self) -> str:
        """Everything that decides how the provider is built, as stable text."""
        return json.dumps(
            {
                "settings": self.settings,
                "residency": residency_record(self.residency),
                "enabled": self.enabled,
            },
            sort_keys=True,
            default=str,
        )

    def to_record(self) -> dict[str, Any]:
        """The connection as stored (``config.yaml`` or the database's JSON columns)."""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "settings": _plain(self.settings),
            "residency": residency_record(self.residency),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Connection:
        """A connection from its stored form. Raises ConnectionInvalid for a malformed one."""
        settings = record.get("settings")
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except ValueError:
                raise ConnectionInvalid("settings is not valid JSON") from None
        if not isinstance(settings, Mapping):
            raise ConnectionInvalid("settings must be a mapping")
        residency = record.get("residency")
        return cls(
            id=str(record.get("id", "")),
            name=str(record.get("name", "")),
            settings=dict(settings),
            residency=(
                residency_from_record(residency)
                if residency is not None
                else default_residency(settings)
            ),
            enabled=bool(record.get("enabled", True)),
        )


def _plain(value: Any) -> Any:
    """``value`` as plain dicts and lists (for YAML and JSON)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class ConnectionModel:
    """A model as one connection offers it: enabled or not, and its credit tier (M5, M7)."""

    connection_id: str
    model_id: str
    enabled: bool = True
    tier: Tier = "standard"


class SettingsValidator(Protocol):
    """The part of conduit's provider registry :func:`validate` uses."""

    def validate(
        self, name: str, settings: ProviderSettings | Mapping[str, Any]
    ) -> ProviderSettings: ...


def validate(connection: Connection, registry: SettingsValidator) -> ProviderSettings:
    """``connection``'s settings, validated by conduit's registry for its kind.

    Refuses a bad id or name, and anything conduit's settings model for the kind
    refuses: an unknown field, a missing one, or a credential that is not a
    ``secret://`` reference. The message names the fields, never a value.
    """
    if not re.fullmatch(CONNECTION_ID_PATTERN, connection.id or ""):
        raise ConnectionInvalid(
            "a connection id is 1 to 63 lower case letters, digits and hyphens, "
            "starting with a letter or digit"
        )
    name = connection.name.strip()
    if not name or len(name) > _NAME_MAX:
        raise ConnectionInvalid(f"a connection name is 1 to {_NAME_MAX} characters")
    problem: str | None = None
    try:
        return registry.validate(connection.id, connection.settings)
    except ConduitError as refused:
        # conduit's messages name fields and kinds, never values.
        problem = str(refused)
    raise ConnectionInvalid(f"connection {connection.id!r}: {problem}")
