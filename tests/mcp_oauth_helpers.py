"""A synthetic OAuth authorisation server for MCP OAuth tests, served in-process.

:class:`SyntheticAuthServer` answers the protected-resource metadata on the MCP
server's own origin and, on its own synthetic origin (``https://auth.example.com``,
never reached over a network), the authorisation server metadata, dynamic
registration, the authorisation endpoint (played by the test as the browser,
:meth:`SyntheticAuthServer.authorise`) and the token endpoint with PKCE checks and
refresh-token rotation. :meth:`transport` is an httpx2 transport that hands those
requests to it and everything else (the MCP session itself) to the network
transport, so a real local MCP stub can sit behind it.

Every value here is synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

ISSUER = "https://auth.example.com"
CLIENT_SECRET = "client-secret-dev-only"  # noqa: S105 - synthetic


@dataclass
class _Grant:
    client_id: str
    redirect_uri: str
    challenge: str
    resource: str | None


@dataclass
class SyntheticAuthServer:
    """See the module. ``resource`` is the MCP server URL the metadata serves."""

    resource: str
    issuer: str = ISSUER
    publish_metadata: bool = True
    registration: bool = True
    promise_iss: bool = True
    expires_in: int = 3600
    clients: dict[str, str | None] = field(default_factory=dict)  # id -> secret
    codes: dict[str, _Grant] = field(default_factory=dict)
    refresh_tokens: dict[str, str] = field(default_factory=dict)  # token -> client
    issued: list[str] = field(default_factory=list)  # every token value issued
    refreshes: int = 0
    token_requests: list[dict[str, str]] = field(default_factory=list)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    _counter: Any = field(default_factory=lambda: itertools.count(1))

    # -- Values the test may look for (in logs, pages, bundles) -------------------------

    def _next(self, kind: str) -> str:
        value = f"{kind}-{next(self._counter):04d}-dev-only"
        self.issued.append(value)
        return value

    def pre_register(self, client_id: str, secret: str | None = None) -> None:
        self.clients[client_id] = secret

    # -- The browser's part -------------------------------------------------------------

    def authorise(self, authorization_url: str, *, deny: bool = False) -> str:
        """The address the authorisation server sends the browser back to."""
        parts = urlsplit(authorization_url)
        assert f"{parts.scheme}://{parts.netloc}" == self.issuer, "not this server"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert query["client_id"] in self.clients, "unknown client"
        back: dict[str, str] = {"state": query["state"]}
        if self.promise_iss:
            back["iss"] = self.issuer
        if deny:
            back["error"] = "access_denied"
        else:
            code = self._next("code")
            self.codes[code] = _Grant(
                query["client_id"],
                query["redirect_uri"],
                query["code_challenge"],
                query.get("resource"),
            )
            back["code"] = code
        return f"{query['redirect_uri']}?{urlencode(back)}"

    # -- The HTTP side --------------------------------------------------------------------

    def _json(self, status: int, data: dict[str, Any]) -> httpx2.Response:
        return httpx2.Response(status, json=data)

    def _metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": self.promise_iss,
        }
        if self.registration:
            data["registration_endpoint"] = f"{self.issuer}/register"
        return data

    def _client_ok(self, form: dict[str, str]) -> bool:
        client = form.get("client_id", "")
        if client not in self.clients:
            return False
        secret = self.clients[client]
        return secret is None or form.get("client_secret") == secret

    def _tokens(self, client: str) -> dict[str, Any]:
        refresh = self._next("refresh")
        self.refresh_tokens[refresh] = client
        return {
            "access_token": self._next("access"),
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
        }

    def _token(self, form: dict[str, str]) -> httpx2.Response:
        self.token_requests.append(dict(form))
        if not self._client_ok(form):
            return self._json(401, {"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            found = self.codes.pop(form.get("code", ""), None)
            if found is None or found.client_id != form["client_id"]:
                return self._json(400, {"error": "invalid_grant"})
            if form.get("redirect_uri") != found.redirect_uri:
                return self._json(400, {"error": "invalid_grant"})
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if challenge != found.challenge:
                return self._json(400, {"error": "invalid_grant"})
            return self._json(200, self._tokens(found.client_id))
        if grant == "refresh_token":
            owner = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
            if owner is None or owner != form["client_id"]:
                return self._json(400, {"error": "invalid_grant"})
            self.refreshes += 1
            return self._json(200, self._tokens(owner))
        return self._json(400, {"error": "unsupported_grant_type"})

    def handle(self, request: httpx2.Request) -> httpx2.Response | None:
        """Answer ``request`` if it is this server's (or the metadata's), else ``None``."""
        url = str(request.url)
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        resource = urlsplit(self.resource)
        if origin == f"{resource.scheme}://{resource.netloc}" and parts.path.startswith(
            "/.well-known/oauth-protected-resource"
        ):
            if not self.publish_metadata:
                return httpx2.Response(404)
            return self._json(
                200, {"resource": self.resource, "authorization_servers": [self.issuer]}
            )
        if origin != self.issuer:
            return None
        if parts.path.startswith("/.well-known/oauth-authorization-server"):
            return self._json(200, self._metadata())
        if parts.path.startswith("/.well-known/"):
            return httpx2.Response(404)
        if parts.path == "/register" and self.registration:
            body = json.loads(request.content or b"{}")
            client = self._next("client")
            self.clients[client] = None
            return self._json(
                201,
                {
                    "client_id": client,
                    "redirect_uris": body.get("redirect_uris", []),
                    "token_endpoint_auth_method": "none",
                },
            )
        if parts.path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return self._token(form)
        return httpx2.Response(404)

    def transport(
        self, inner: httpx2.AsyncBaseTransport | None = None
    ) -> httpx2.AsyncBaseTransport:
        """An httpx2 transport answering this server's requests, the rest by ``inner``."""
        return _Routed(self, inner)


class _Routed(httpx2.AsyncBaseTransport):
    def __init__(self, server: SyntheticAuthServer, inner: httpx2.AsyncBaseTransport | None):
        self._server = server
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self._server.requests.append((str(request.url), dict(request.headers)))
        answer = self._server.handle(request)
        if answer is not None:
            return answer
        if self._inner is None:
            return httpx2.Response(404)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


class MemorySecrets:
    """konfig's secrets store, in memory; ``sets`` counts every write."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.sets = 0

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.sets += 1
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self.data

    def list_keys(self) -> list[str]:
        return list(self.data)

    def resolve_uri(self, value: str) -> str | None:
        return self.data.get(value[len("secret://") :]) if value.startswith("secret://") else value
