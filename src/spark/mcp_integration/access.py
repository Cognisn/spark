"""Server mode: a signed-in request holds ``spark.mcp.<server>`` for the servers granted to its roles.

warden defines roles once, at start-up, and an MCP server is administrator
data that comes and goes at run time, so the grants are kept in the
``mcp.servers`` instance setting (each entry's ``roles``) and resolved here, on
every request, after warden has admitted it (A12). The request's
:class:`~spark.identity.access.SparkIdentity` is replaced by one whose
permissions also hold the granted servers, so everything that hands the
request's permissions on (the tools configuration, a turn) carries them.

A request authenticated by an API key gains no grant: a key's scopes are fixed
when it is made and cannot name a server that is only a setting. Personal mode
has one local operator and no grants: nothing changes.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from starlette.requests import HTTPConnection

from spark.mcp_integration.servers import grant_permissions

__all__ = ["apply_grants"]


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


def _via_api_key(request: HTTPConnection | Any) -> bool:
    from spark.identity.web import via_api_key

    return via_api_key(request)


def apply_grants(request: HTTPConnection | Any) -> None:
    """Add the MCP grants of the request's roles to its identity (server mode only)."""
    if not _server():
        return
    from spark.identity.access import STATE_KEY, SparkIdentity

    identity = getattr(request.state, STATE_KEY, None)
    if not isinstance(identity, SparkIdentity) or _via_api_key(request):
        return
    source = getattr(request.app.state, "instance_settings", None)
    if source is None:
        return
    servers = source.get("mcp.servers")
    granted = grant_permissions(servers if isinstance(servers, list) else [], identity.roles)
    if granted - identity.permissions:
        setattr(
            request.state,
            STATE_KEY,
            dataclasses.replace(identity, permissions=identity.permissions | granted),
        )
