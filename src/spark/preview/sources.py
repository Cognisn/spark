"""Where a link may come from: the hosts of the MCP servers the sender is connected to.

The preview's fetch guard (:mod:`spark.preview.guard`) takes the list this module
gathers. A server counts when it is registered, connected, reached over HTTP (not
stdio), and, on a server, granted to the sender by their roles (the tool decision
point, :func:`spark.tools.registry.is_mcp_server_available`, through
:meth:`spark.mcp_integration.manager.MCPManager.servers_for`). Each server's host is
resolved here, once per gathering, so the guard knows the address the MCP server is
at (rule 4).

The process's MCP manager lives on the web app (``app.state.mcp_manager``), which a
tool on a turn's worker thread cannot see: :func:`bind` (called once when the routes
are registered) records where to find it.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from spark.preview.guard import McpOrigin, Resolver, default_resolver

__all__ = ["bind", "bound_manager", "mcp_origins", "origins_for", "unbind"]

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}

_manager_source: Callable[[], Any] | None = None


def bind(source: Callable[[], Any] | None) -> None:
    """Record how to find the process's MCP manager (None forgets it)."""
    global _manager_source
    _manager_source = source


def unbind() -> None:
    """Forget the MCP manager (tests)."""
    bind(None)


def bound_manager() -> Any:
    """The process's MCP manager, or None (no servers configured, or not started)."""
    source = _manager_source
    if source is None:
        return None
    try:
        return source()
    except Exception:  # noqa: BLE001 - no manager means no allowed hosts
        logger.warning("The MCP manager could not be found for the preview")
        return None


def _addresses(host: str, port: int, resolver: Resolver) -> frozenset[str]:
    try:
        return frozenset({str(ipaddress.ip_address(host))})
    except ValueError:
        return frozenset(resolver(host, port))


def mcp_origins(
    manager: Any, permissions: Any, *, resolver: Resolver = default_resolver
) -> list[McpOrigin]:
    """The origins of the MCP servers ``permissions`` grant that are connected over HTTP."""
    if manager is None:
        return []
    try:
        clients = manager.servers_for(permissions)
    except Exception:  # noqa: BLE001 - a broken manager allows nothing
        logger.warning("The MCP servers could not be listed for the preview")
        return []
    found: list[McpOrigin] = []
    for name, client in sorted(clients.items()):
        if not getattr(client, "connected", False):
            continue
        config = getattr(client, "config", None)
        if config is None or getattr(config, "transport", "stdio") not in ("http", "sse"):
            continue
        parts = urlsplit(str(getattr(config, "url", "") or ""))
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").rstrip(".").lower()
        if scheme not in _DEFAULT_PORTS or not host:
            continue
        try:
            port = parts.port or _DEFAULT_PORTS[scheme]
        except ValueError:
            continue
        found.append(
            McpOrigin(
                name=str(name),
                scheme=scheme,
                host=host,
                port=int(port),
                addresses=_addresses(host, int(port), resolver),
                ssl_verify=bool(getattr(config, "ssl_verify", True)),
            )
        )
    return found


def origins_for(permissions: Any, *, resolver: Resolver = default_resolver) -> list[McpOrigin]:
    """The origins of the bound manager's servers for a sender holding ``permissions``."""
    return mcp_origins(bound_manager(), permissions, resolver=resolver)
