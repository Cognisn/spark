"""The fetch guard: how SPARK's server fetches a document link for the preview (CS37).

The AI asks for a link to be shown; SPARK's server fetches it, never the browser,
and only under these rules. Each refusal raises :class:`LinkRefused` with a plain
reason the model is told.

1. **Scheme.** ``https``, or the scheme the matching MCP server itself uses (an
   ``http`` server on an internal network). Anything else is refused.
2. **Host.** The link's host must exactly match the host of one of the MCP servers
   the sender is connected to (case and a trailing dot aside), on that server's port
   (or the scheme's default port for ``https`` when the server uses ``http``).
   A link that names a user or password is refused.
3. **One lookup, pinned.** The host name is resolved once; the connection is made to
   that address, with the host name kept for the ``Host`` header, TLS server name
   and certificate check, so a second lookup cannot send it elsewhere.
4. **Private addresses.** An address that is not public (private, loopback,
   link-local, unique-local, shared, reserved, multicast or unspecified) is refused
   unless the MCP server itself is at that address: the address the server's host
   resolved to when the allowed hosts were gathered (:mod:`spark.preview.sources`).
5. **Redirects.** Followed only to the same scheme, host and port (at most
   :data:`MAX_REDIRECTS`), on the pinned address; a redirect to another host is
   refused.
6. **Size.** Refused past ``max_bytes`` (by default the attachment limit), whether
   the response declares its length or not; nothing is kept.
7. **Time.** The whole fetch, redirects included, must finish within ``timeout``
   seconds; past it the connection is closed and the fetch refused.
8. **Type.** The response's declared type is checked against its content (magic
   bytes, :func:`spark.preview.kinds.content_matches`); a mismatch is refused. A
   compressed response (``Content-Encoding`` other than identity) is refused.
9. **No credentials.** The request carries only what the URL carries: no cookies
   (SPARK's or the link's own, which are never kept), no ``Authorization``, no proxy
   or netrc settings from the environment. Only ``User-Agent``, ``Accept`` and
   ``Accept-Encoding: identity`` are sent.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from spark.preview.kinds import GENERIC_TYPES, Kind, base_type, classify, content_matches

__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_REDIRECTS",
    "Fetched",
    "LinkRefused",
    "McpOrigin",
    "Resolver",
    "TransportFactory",
    "default_resolver",
    "fetch",
    "is_public_address",
    "match_origin",
]

DEFAULT_TIMEOUT: Final = 30.0
MAX_REDIRECTS: Final = 3
USER_AGENT: Final = "SPARK document preview"
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_CHUNK: Final = 64 * 1024

# host name, port -> the addresses it resolves to (strings).
Resolver = Callable[[str, int], list[str]]
# verify (True, False or an SSL context) -> an httpx transport.
TransportFactory = Callable[[Any], httpx.BaseTransport]


class LinkRefused(Exception):
    """The link may not be fetched; the message says why, plainly."""


@dataclass(frozen=True)
class McpOrigin:
    """Where one of the sender's MCP servers is: its scheme, host, port and addresses."""

    name: str
    scheme: str
    host: str
    port: int
    addresses: frozenset[str] = field(default_factory=frozenset)
    ssl_verify: bool = True


@dataclass(frozen=True)
class Fetched:
    """A fetched document: its bytes, its declared and treated type, its name."""

    content: bytes
    content_type: str
    media_type: str
    kind: Kind
    filename: str
    url: str


def default_resolver(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (one lookup)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    found: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if address not in found:
            found.append(address)
    return found


def _default_transport(verify: Any) -> httpx.BaseTransport:
    return httpx.HTTPTransport(verify=verify, retries=0, trust_env=False)


def is_public_address(address: str) -> bool:
    """Whether ``address`` is a public unicast address (not private, loopback,
    link-local, unique-local, shared, reserved, multicast or unspecified)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def _normal_host(host: str | None) -> str:
    return (host or "").strip().rstrip(".").lower()


def _port(parts: Any) -> int | None:
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None:
        return int(port)
    return _DEFAULT_PORTS.get(parts.scheme.lower())


def match_origin(url: str, origins: Iterable[McpOrigin]) -> McpOrigin:
    """The MCP server whose host ``url`` names (rules 1 and 2), or :class:`LinkRefused`."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise LinkRefused("Only https links (or the scheme of your MCP server) can be shown.")
    if parts.username is not None or parts.password is not None:
        raise LinkRefused("Links that carry a user name or password are not fetched.")
    host = _normal_host(parts.hostname)
    port = _port(parts)
    if not host or port is None:
        raise LinkRefused("The link has no usable host.")
    known = list(origins)
    if not known:
        raise LinkRefused(
            "Links can be shown only from MCP servers you are connected to, and you are "
            "connected to none."
        )
    for origin in known:
        if _normal_host(origin.host) != host:
            continue
        if scheme == origin.scheme:
            if port == origin.port:
                return origin
        elif scheme == "https" and port == _DEFAULT_PORTS["https"]:
            return origin
    if any(_normal_host(o.host) == host for o in known):
        raise LinkRefused(
            "The link uses a scheme or port that its MCP server does not use, so it is not "
            "fetched."
        )
    raise LinkRefused(
        "Links can be shown only from the hosts of MCP servers you are connected to; this "
        "link's host is not one of them."
    )


def _pin(origin: McpOrigin, host: str, port: int, resolver: Resolver) -> str:
    """The one address to connect to (rules 3 and 4)."""
    addresses = resolver(host, port)
    if not addresses:
        raise LinkRefused("The link's host could not be found.")
    for address in addresses:
        if is_public_address(address) or address in origin.addresses:
            return address
    raise LinkRefused(
        "The link's host resolves to a private or local address that its MCP server is "
        "not at, so it is not fetched."
    )


def _address_url(scheme: str, address: str, port: int, parts: Any) -> str:
    shown = f"[{address}]" if ":" in address else address
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{shown}:{port}{path}{query}"


def _host_header(host: str, scheme: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return shown if port == _DEFAULT_PORTS[scheme] else f"{shown}:{port}"


_FILENAME_STAR: Final = re.compile(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", re.I)
_FILENAME: Final = re.compile(r'filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))', re.I)


def disposition_filename(header: str | None) -> str | None:
    """The file name a ``Content-Disposition`` header gives, or None."""
    if not header:
        return None
    star = _FILENAME_STAR.search(header)
    if star:
        try:
            return unquote(star.group(2).strip(), encoding=star.group(1).strip() or "utf-8")
        except LookupError:
            return unquote(star.group(2).strip())
    plain = _FILENAME.search(header)
    if plain:
        value = plain.group(1) if plain.group(1) is not None else plain.group(2)
        return re.sub(r"\\(.)", r"\1", value).strip()
    return None


def _url_filename(url: str) -> str | None:
    last = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return last or None


class _Deadline:
    """Closes the response being read when the fetch runs out of time (rule 7)."""

    def __init__(self, seconds: float) -> None:
        self.ends = time.monotonic() + seconds
        self.expired = False
        self._response: httpx.Response | None = None
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(seconds, 0.0), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def left(self) -> float:
        left = self.ends - time.monotonic()
        if left <= 0:
            self.expired = True
            raise LinkRefused("The link did not answer in time, so it is not shown.")
        return left

    def watch(self, response: httpx.Response | None) -> None:
        with self._lock:
            self._response = response

    def _expire(self) -> None:
        self.expired = True
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    def cancel(self) -> None:
        self._timer.cancel()


def _read(response: httpx.Response, max_bytes: int | None, deadline: _Deadline) -> bytes:
    declared = response.headers.get("content-length", "")
    if max_bytes is not None and declared.isdigit() and int(declared) > max_bytes:
        raise LinkRefused(
            f"The document is larger than the attachment limit of {max_bytes:,} bytes."
        )
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_raw(_CHUNK):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise LinkRefused(
                    f"The document is larger than the attachment limit of {max_bytes:,} bytes."
                )
            chunks.append(chunk)
            deadline.left()
    except LinkRefused:
        raise
    except Exception as exc:
        if deadline.expired:
            raise LinkRefused("The link did not answer in time, so it is not shown.") from exc
        raise LinkRefused("The link's answer could not be read.") from exc
    if deadline.expired:
        raise LinkRefused("The link did not answer in time, so it is not shown.")
    return b"".join(chunks)


def _verify(origin: McpOrigin) -> Any:
    if not origin.ssl_verify:
        return False
    return ssl.create_default_context()


def fetch(
    url: str,
    origins: Sequence[McpOrigin],
    *,
    max_bytes: int | None,
    timeout: float = DEFAULT_TIMEOUT,
    resolver: Resolver = default_resolver,
    transport_factory: TransportFactory = _default_transport,
) -> Fetched:
    """Fetch ``url`` under every rule above; raises :class:`LinkRefused`."""
    origin = match_origin(url, origins)
    current = url.strip()
    parts = urlsplit(current)
    scheme = parts.scheme.lower()
    host = _normal_host(parts.hostname)
    port = _port(parts)
    assert port is not None  # match_origin refused a link without one
    address = _pin(origin, host, port, resolver)
    deadline = _Deadline(timeout)
    transport = transport_factory(_verify(origin) if scheme == "https" else True)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            request = httpx.Request(
                "GET",
                _address_url(scheme, address, port, parts),
                headers={
                    "Host": _host_header(host, scheme, port),
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
                extensions={
                    "timeout": {
                        "connect": deadline.left(),
                        "read": deadline.left(),
                        "write": deadline.left(),
                        "pool": deadline.left(),
                    },
                    **({"sni_hostname": host} if scheme == "https" else {}),
                },
            )
            try:
                response = transport.handle_request(request)
            except LinkRefused:
                raise
            except Exception as exc:
                if deadline.expired or isinstance(exc, httpx.TimeoutException):
                    raise LinkRefused("The link did not answer in time, so it is not shown.")
                raise LinkRefused("The link could not be reached.") from exc
            deadline.watch(response)
            try:
                if response.status_code in _REDIRECTS:
                    location = response.headers.get("location", "")
                    target = urljoin(current, location) if location else ""
                    nxt = urlsplit(target)
                    if (
                        not target
                        or nxt.scheme.lower() != scheme
                        or _normal_host(nxt.hostname) != host
                        or _port(nxt) != port
                        or nxt.username is not None
                        or nxt.password is not None
                    ):
                        raise LinkRefused(
                            "The link redirects to another host, so it is not followed."
                        )
                    current = target
                    continue
                if response.status_code != 200:
                    raise LinkRefused(
                        f"The link answered {response.status_code}, not the document."
                    )
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise LinkRefused("The link sent a compressed answer, which is not shown.")
                content = _read(response, max_bytes, deadline)
            finally:
                deadline.watch(None)
                response.close()
            return _typed(content, response.headers, current)
        raise LinkRefused("The link redirects too many times, so it is not followed.")
    finally:
        deadline.cancel()
        transport.close()


def _typed(content: bytes, headers: httpx.Headers, url: str) -> Fetched:
    """The fetched document with its type checked against its content (rule 8)."""
    declared = headers.get("content-type", "")
    name = (
        disposition_filename(headers.get("content-disposition")) or _url_filename(url) or "document"
    )
    kind, media = classify(declared, name)
    if not content_matches(kind, media, content):
        if base_type(declared) in GENERIC_TYPES:
            # The name suggested a type the content is not: offered for download only.
            kind, media = Kind.OTHER, base_type(declared) or "application/octet-stream"
        else:
            raise LinkRefused(
                f"The document's declared type ({base_type(declared)}) does not match its "
                "content, so it is not shown."
            )
    return Fetched(content, declared, media, kind, name, url)
