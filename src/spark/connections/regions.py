"""Choosing the Regions a connection may use (conduit 0.1.0).

An administrator chooses, for each connection, where its requests may be processed.
conduit offers the choices (``ProviderRegistry.regions(kind)``: documented, static lists
of Regions and geographies) and each adapter takes the choice in its own settings, which
conduit validates into a ``RegionChoice`` (``ProviderSettings.region_choice()``):

=====================  ===========================================  ==================
Service                Setting                                      Choice
=====================  ===========================================  ==================
``bedrock``            ``processing`` (AWS Regions, allow_global)   several Regions
``anthropic``          ``inference_geo`` (``us`` or ``global``)     one geography
``openai``             ``service.data_residency`` (a project's)     one Region
``anthropic_foundry``  ``deployment`` (type and Azure region)       one deployment
``openai_foundry``     ``service.deployment`` (type, Azure region)  one deployment
=====================  ===========================================  ==================

Any other service offers no choice. The choice only narrows what the deployment's
residency policy already allows (conduit holds a model to both), and a connection with
none behaves as it always did.

:func:`offered` lists what a connection may choose, :func:`current` what it has chosen,
and :func:`with_choice` the connection's settings with a choice applied (or cleared). It
checks the shape of a choice; conduit validates the settings it builds
(:meth:`spark.connections.runtime.ConnectionRuntime.validate`) and its refusal, which
names fields and never a value, is what the administrator is shown.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Final

from conduit import ProviderRegistry, RegionListing

from spark.connections.model import ConnectionInvalid, service_of

__all__ = ["MODES", "current", "mode_of", "offered", "with_choice"]

#: How each service takes a Region choice (absent: it offers none).
MODES: Final[Mapping[str, str]] = {
    "bedrock": "processing",
    "anthropic": "inference_geo",
    "openai": "data_residency",
    "anthropic_foundry": "deployment",
    "openai_foundry": "deployment",
}

#: The listing (conduit's ``RegionListing.service``) each service reads its choices from.
_LISTING: Final[Mapping[str, str | None]] = {
    "bedrock": None,
    "anthropic": "direct",
    "openai": "openai",
    "anthropic_foundry": "foundry",
    "openai_foundry": "foundry",
}

_GEOGRAPHIES: Final = ("us", "global")
_DEPLOYMENT_TYPES: Final = ("global", "data_zone", "standard")


def mode_of(service: str | None) -> str | None:
    """How ``service`` takes a Region choice, or None when it offers none."""
    return MODES.get(service or "")


def _listing_dict(listing: RegionListing) -> dict[str, Any]:
    return {
        "kind": listing.kind,
        "service": listing.service,
        "global_option": listing.global_option,
        "documentation": listing.documentation,
        "as_of": listing.as_of,
        "regions": [
            {
                "code": r.code,
                "name": r.name,
                "invocation": r.invocation,
                "scope": r.scope.value,
                "members": sorted(r.members),
                "stores_only": r.stores_only,
            }
            for r in listing.regions
        ],
    }


def offered(registry: ProviderRegistry, settings: Mapping[str, Any]) -> dict[str, Any]:
    """What a connection with ``settings`` may choose: how its service takes a choice
    and conduit's listings of Regions for it (empty when it offers none)."""
    service = service_of(settings)
    mode = mode_of(service)
    if mode is None or service is None:
        return {"mode": None, "listings": []}
    wanted = _LISTING[service]
    listings = [
        _listing_dict(listing)
        for listing in registry.regions(str(settings.get("kind")))
        if listing.service == wanted
    ]
    return {"mode": mode, "listings": listings}


def _azure(code: str) -> str:
    return code.removeprefix("azure:")


def current(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    """The Region choice ``settings`` hold, in the shape :func:`with_choice` takes
    (None for a connection that has chosen none)."""
    service = service_of(settings)
    if service == "bedrock":
        chosen = settings.get("processing")
        if isinstance(chosen, Mapping):
            return {
                "regions": sorted(str(r) for r in chosen.get("regions") or ()),
                "allow_global": bool(chosen.get("allow_global", False)),
            }
    elif service == "anthropic":
        geo = settings.get("inference_geo")
        if geo in _GEOGRAPHIES:
            return {"geography": geo}
    elif service == "openai":
        inner = settings.get("service")
        code = inner.get("data_residency") if isinstance(inner, Mapping) else None
        if isinstance(code, str) and code:
            return {"regions": [f"openai:{code}"]}
    elif service in ("anthropic_foundry", "openai_foundry"):
        holder = settings if service == "anthropic_foundry" else settings.get("service")
        deployment = holder.get("deployment") if isinstance(holder, Mapping) else None
        if isinstance(deployment, Mapping):
            return {
                "deployment": {
                    "type": deployment.get("type"),
                    "azure_region": deployment.get("azure_region"),
                }
            }
    return None


def _text_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v for v in value):
        raise ConnectionInvalid(f"{field} must be a list of Region codes")
    return [str(v) for v in value]


def with_choice(settings: Mapping[str, Any], choice: Mapping[str, Any] | None) -> dict[str, Any]:
    """``settings`` with ``choice`` applied: the adapter's own setting set, or removed for
    an empty choice. Raises :class:`ConnectionInvalid` for a service that offers none or a
    choice that is malformed; whether conduit accepts the values is conduit's to say."""
    service = service_of(settings)
    mode = mode_of(service)
    if mode is None:
        raise ConnectionInvalid("this service offers no Region choice")
    result = copy.deepcopy(dict(settings))
    empty = not choice or not any(
        choice.get(k) for k in ("regions", "geography", "deployment", "allow_global")
    )
    if mode == "processing":
        if empty:
            result.pop("processing", None)
        else:
            regions = _text_list((choice or {}).get("regions") or [], "regions")
            result["processing"] = {
                "regions": regions,
                "allow_global": bool((choice or {}).get("allow_global", False)),
            }
    elif mode == "inference_geo":
        if empty:
            result.pop("inference_geo", None)
        else:
            geography = (choice or {}).get("geography")
            if geography not in _GEOGRAPHIES:
                raise ConnectionInvalid("geography must be us or global")
            result["inference_geo"] = geography
    elif mode == "data_residency":
        inner = dict(result.get("service") or {})
        if empty:
            inner.pop("data_residency", None)
        else:
            regions = _text_list((choice or {}).get("regions") or [], "regions")
            if len(regions) != 1:
                raise ConnectionInvalid("choose one OpenAI data residency Region")
            inner["data_residency"] = regions[0].removeprefix("openai:")
        result["service"] = inner
    else:  # deployment
        holder = result if service == "anthropic_foundry" else dict(result.get("service") or {})
        if empty:
            holder.pop("deployment", None)
        else:
            deployment = (choice or {}).get("deployment")
            if not isinstance(deployment, Mapping):
                raise ConnectionInvalid("deployment must give a type and an Azure region")
            kind = deployment.get("type")
            region = deployment.get("azure_region")
            if kind not in _DEPLOYMENT_TYPES or not isinstance(region, str) or not region:
                raise ConnectionInvalid(
                    "deployment must give a type (global, data_zone or standard) and an Azure region"
                )
            holder["deployment"] = {"type": kind, "azure_region": _azure(region)}
        if service == "openai_foundry":
            result["service"] = holder
    return result
