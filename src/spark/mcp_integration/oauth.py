"""OAuth for MCP servers: SPARK's token store and sign-in hook for conduit's client (C14).

conduit's :class:`conduit.mcp.OAuthClient` does the OAuth work: discovery with its
audience and issuer checks, dynamic registration, the authorisation code with PKCE,
the callback's redirect, ``state`` and issuer checks, and refresh rotation. SPARK
supplies what conduit asks of a product:

* **Whose tokens.** One token set, and one dynamic registration, per configured
  server for the whole instance, signed in by whoever administers it. Every user
  the server is granted to (``spark.mcp.<server>``) calls it with those tokens, as
  with a configured bearer token.
* **The token store** (:class:`SecretsTokenStore`), over konfig's secrets store: the
  keychain in personal mode, the encrypted file or AWS in server mode. Each value is
  written in parts of at most :data:`PART_LIMIT` characters (the Windows credential
  store holds little more than a thousand), as a new generation before the head
  entry moves to it, so a reader sees the old set or the new one, never a mixture.
  There is one store object per server per secrets store in the process
  (:func:`token_store`): conduit's lock is per store object and key, so every
  client of a server, the sign-in's included, shares one lock and one refresh.
* **The sign-in hook** (:class:`WebSignIn`) and the pending sign-ins
  (:class:`SignIns`). The administrator starts a sign-in from the settings page; the
  hook hands conduit's authorisation URL back to the page, which sends the browser
  there; the authorisation server returns the browser to :data:`CALLBACK_PATH`,
  under the instance's own address, and the callback hands the full URL to the
  waiting hook, which conduit then checks. The return is a cross-site navigation, so
  it carries no session cookie (they are all ``SameSite=Strict``): the callback is
  public, and it is bound to the administrator's browser by :data:`BINDING_COOKIE`
  (``SameSite=Lax``, HttpOnly), set when the sign-in starts, holding a 256-bit
  nonce for one pending sign-in that expires after :data:`SIGN_IN_SECONDS` and is
  used once. A callback without the matching cookie changes nothing.

Tokens, registrations, codes and the state are never logged, shown or exported:
the store's values are only ever in konfig, its repr names the server alone, and a
failure is conduit's failure class (:class:`conduit.mcp.OAuthFailure`), never text.
Everything in :class:`SignIns` runs on the process's conduit loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, urlsplit

from conduit.core.backends import TokenSet
from conduit.mcp import McpAuthError, OAuthClient, SignInRequest, SignInResult

__all__ = [
    "BINDING_COOKIE",
    "CALLBACK_PATH",
    "PART_LIMIT",
    "SIGN_IN_SECONDS",
    "SecretsTokenStore",
    "SignIns",
    "SignInStarted",
    "Outcome",
    "WebSignIn",
    "REASONS",
    "forget",
    "oauth_status",
    "reason_text",
    "token_store",
]

logger = logging.getLogger(__name__)

#: Where the authorisation server returns the browser (public: see the module).
CALLBACK_PATH: Final = "/mcp/oauth/callback"
#: The cookie binding a callback to the browser that started the sign-in.
BINDING_COOKIE: Final = "spark-mcp-oauth"
#: How long a started sign-in waits for its callback.
SIGN_IN_SECONDS: Final = 600
#: The longest value written to one secrets-store entry.
PART_LIMIT: Final = 1000
#: How long starting a sign-in waits for conduit's discovery and registration.
_START_SECONDS: Final = 60.0
_FORMAT: Final = "spark.mcp.oauth/1"
_KEY_LIMIT: Final = 512


class SecretsLike(Protocol):
    """The part of konfig's ``Secrets`` the token store uses."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class TokenStoreError(RuntimeError):
    """The store could not read or write (conduit reports ``store``). Names no value."""


def entry_name(server: str, key: str) -> str:
    """The secrets-store name of ``server``'s record for conduit's ``key``.

    ``server`` is SPARK's name; the key is kept only as a digest, so the name stays
    within what every konfig backend accepts.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"mcp_server_{server}_oauth.{digest}"


def _keys(server: str) -> tuple[str, str]:
    """conduit's token and registration keys for SPARK's server ``server``."""
    from spark.mcp_integration.client import conduit_name

    name = conduit_name(server)
    return f"mcp:{name}", f"mcp:{name}:client"


class SecretsTokenStore:
    """conduit's ``TokenStore`` for one MCP server, over konfig's secrets store.

    conduit names its keys ``mcp:<conduit name>`` (tokens) and
    ``mcp:<conduit name>:client`` (a dynamic registration). This store belongs to one
    SPARK server and names every record by SPARK's name, so two servers whose conduit
    names collide never share a record. konfig's calls block, so each runs in a
    worker thread.

    ``last_failure`` is conduit's failure class from this server's last OAuth
    failure in this process, or ``None`` once it has worked.
    """

    def __init__(self, secrets: SecretsLike, server: str) -> None:
        self._secrets = secrets
        self.server = server
        self.last_failure: str | None = None

    def __repr__(self) -> str:
        return f"SecretsTokenStore({self.server!r})"

    def _name(self, key: str) -> str:
        if not isinstance(key, str) or not key or len(key) > _KEY_LIMIT:
            raise TokenStoreError("not a key this store holds")
        return entry_name(self.server, key)

    # -- Synchronous record handling (worker threads) ---------------------------------

    def _head(self, name: str) -> dict[str, Any] | None:
        raw = self._secrets.get(name)
        if not raw:
            return None
        head: Any = None
        try:
            head = json.loads(raw)
        except ValueError:
            head = None
        if (
            not isinstance(head, dict)
            or head.get("format") != _FORMAT
            or not isinstance(head.get("generation"), str)
            or not isinstance(head.get("parts"), int)
            or head["parts"] < 1
        ):
            raise TokenStoreError("a stored OAuth record is not readable")
        return head

    @staticmethod
    def _part(name: str, generation: str, index: int) -> str:
        return f"{name}.{generation}.{index}"

    def _read(self, name: str) -> TokenSet | None:
        head = self._head(name)
        if head is None:
            return None
        chunks: list[str] = []
        for index in range(head["parts"]):
            chunk = self._secrets.get(self._part(name, head["generation"], index))
            if chunk is None:
                raise TokenStoreError("a stored OAuth record is incomplete")
            chunks.append(chunk)
        tokens: TokenSet | None = None
        try:
            tokens = TokenSet.from_storage(json.loads("".join(chunks)))
        except (ValueError, TypeError):
            tokens = None  # the decoder's message would repeat the values
        if tokens is None:
            raise TokenStoreError("a stored OAuth record is not readable")
        return tokens

    def _drop_parts(self, name: str, head: dict[str, Any] | None) -> None:
        if head is None:
            return
        for index in range(head["parts"]):
            self._secrets.delete(self._part(name, head["generation"], index))

    def _write(self, name: str, tokens: TokenSet) -> None:
        try:
            previous = self._head(name)
        except TokenStoreError:
            previous = None  # an unreadable record is replaced
        text = json.dumps(tokens.to_storage(), ensure_ascii=False)
        chunks = [text[i : i + PART_LIMIT] for i in range(0, len(text), PART_LIMIT)] or [""]
        generation = secrets.token_hex(8)
        for index, chunk in enumerate(chunks):
            self._secrets.set(self._part(name, generation, index), chunk)
        head = {"format": _FORMAT, "generation": generation, "parts": len(chunks)}
        self._secrets.set(name, json.dumps(head))
        self._drop_parts(name, previous)

    def _remove(self, name: str) -> None:
        try:
            head = self._head(name)
        except TokenStoreError:
            head = None
        self._secrets.delete(name)
        self._drop_parts(name, head)

    # -- conduit's TokenStore -----------------------------------------------------------

    async def get(self, key: str) -> TokenSet | None:
        return await asyncio.to_thread(self._read, self._name(key))

    async def put(self, key: str, tokens: TokenSet) -> None:
        await asyncio.to_thread(self._write, self._name(key), tokens)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._remove, self._name(key))

    # -- SPARK's own questions ----------------------------------------------------------

    def signed_in(self) -> bool:
        """Whether tokens are held (they may need a refresh). Reads no token value."""
        try:
            return self._head(entry_name(self.server, _keys(self.server)[0])) is not None
        except TokenStoreError:
            return False
        except Exception:  # noqa: BLE001 - a store that cannot be read holds nothing usable
            return False

    def forget(self) -> None:
        """Remove the tokens and the registration (the server was removed)."""
        for key in _keys(self.server):
            self._remove(entry_name(self.server, key))
        self.last_failure = None


_Stores = dict[str, SecretsTokenStore]
#: One store per (secrets store, server) in the process; see the module.
_BY_STORE: weakref.WeakKeyDictionary[Any, _Stores] = weakref.WeakKeyDictionary()
_PINNED: dict[int, tuple[Any, _Stores]] = {}


def _stores(secrets: SecretsLike) -> _Stores:
    try:
        found = _BY_STORE.get(secrets)
        if found is None:
            found = {}
            _BY_STORE[secrets] = found
        return found
    except TypeError:
        pinned = _PINNED.setdefault(id(secrets), (secrets, {}))
        return pinned[1]


def token_store(secrets: SecretsLike, server: str) -> SecretsTokenStore:
    """The one token store of ``server`` over ``secrets`` in this process."""
    stores = _stores(secrets)
    store = stores.get(server)
    if store is None:
        store = SecretsTokenStore(secrets, server)
        stores[server] = store
    return store


def forget(secrets: SecretsLike, server: str) -> None:
    """Remove ``server``'s tokens and registration, and its store object."""
    token_store(secrets, server).forget()
    _stores(secrets).pop(server, None)


#: What each failure class means, for the settings page (conduit's classes, then SPARK's).
REASONS: Final[dict[str, str]] = {
    "insecure_endpoint": "An OAuth endpoint the server names is not https.",
    "discovery": (
        "The server publishes no usable OAuth discovery metadata "
        "(protected resource and authorisation server metadata)."
    ),
    "audience_mismatch": "The server's OAuth metadata names another resource.",
    "issuer_mismatch": "The authorisation server is not the issuer configured or discovered.",
    "pkce_unsupported": "The authorisation server does not offer PKCE with S256.",
    "registration": (
        "Dynamic client registration failed: the authorisation server may not offer "
        "it, so enter the client id it issued."
    ),
    "redirect_mismatch": "The sign-in did not return to this instance's callback address.",
    "state_mismatch": "The sign-in's answer did not match the sign-in that was started.",
    "authorisation_denied": "The authorisation server refused the sign-in.",
    "token_exchange": "The authorisation server did not issue tokens for the sign-in.",
    "refresh": "The tokens could not be refreshed.",
    "sign_in_required": "Sign in to this server.",
    "sign_in_failed": "The sign-in was not completed in time, or was abandoned.",
    "store": "The secrets store could not read or write the tokens.",
    "issuer_changed": "The server now names another authorisation server: sign in again.",
    "token_rejected": "The server refused the tokens it was given: sign in again.",
    "unknown_sign_in": (
        "No sign-in was waiting for this browser. Start the sign-in again from the "
        "settings page, in this browser."
    ),
    "expired": "The sign-in took too long. Start it again from the settings page.",
}


def reason_text(reason: str | None) -> str:
    """The settings page's sentence for a failure class (never the server's own text)."""
    if not reason:
        return ""
    return REASONS.get(reason, "The OAuth sign-in failed.")


def oauth_status(store: SecretsTokenStore) -> dict[str, Any]:
    """The settings page's status: ``signed_in``, ``needs_sign_in`` or ``failed``.

    ``failed`` carries conduit's failure class as ``reason`` and its sentence as
    ``message``.
    """
    failure = store.last_failure
    if failure is not None and failure != "sign_in_required":
        return {"state": "failed", "reason": failure, "message": reason_text(failure)}
    state = "signed_in" if store.signed_in() else "needs_sign_in"
    return {"state": state, "reason": None, "message": ""}


# -- The sign-in ----------------------------------------------------------------------------


def _single_state(url: str) -> str | None:
    states = parse_qs(urlsplit(url).query, keep_blank_values=True).get("state", [])
    return states[0] if len(states) == 1 and states[0] else None


@dataclass
class _Pending:
    nonce: str = field(repr=False)
    server: str
    actor: dict[str, Any] = field(repr=False)
    started: float
    redirect: asyncio.Future[str] = field(repr=False)
    callback: asyncio.Future[str] = field(repr=False)
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


class WebSignIn:
    """conduit's sign-in hook for one pending sign-in through the web interface.

    ``redirect_uri`` is the callback under the instance's own address. :meth:`sign_in`
    hands conduit's authorisation URL to the page (through the pending sign-in) and
    waits for the callback URL the browser brings back.
    """

    def __init__(self, redirect_uri: str, pending: _Pending, timeout: float) -> None:
        self.redirect_uri = redirect_uri
        self._pending = pending
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"WebSignIn({self._pending.server!r})"

    async def sign_in(self, request: SignInRequest) -> SignInResult:
        if _single_state(request.authorization_url) is None:
            raise RuntimeError("an authorisation URL without exactly one state")
        pending = self._pending
        if not pending.redirect.done():
            pending.redirect.set_result(request.authorization_url)
        url = await asyncio.wait_for(asyncio.shield(pending.callback), self._timeout)
        return SignInResult(callback_url=url)


@dataclass(frozen=True)
class SignInStarted:
    """What starting a sign-in gave: a redirect, or already an outcome."""

    state: str  # redirect | signed_in | failed
    authorization_url: str | None = field(default=None, repr=False)
    nonce: str | None = field(default=None, repr=False)
    reason: str | None = None


@dataclass(frozen=True)
class Outcome:
    """How a callback ended. ``reason`` is conduit's failure class, or SPARK's own
    (``unknown_sign_in``, ``expired``)."""

    signed_in: bool
    server: str | None = None
    reason: str | None = None
    actor: dict[str, Any] = field(default_factory=dict, repr=False)


def _reason(exc: BaseException) -> str:
    """conduit's failure class for ``exc``; anything else is a failed sign-in."""
    return exc.reason.value if isinstance(exc, McpAuthError) else "sign_in_failed"


class SignIns:
    """The pending sign-ins of this process, at most one per server (conduit loop only)."""

    def __init__(
        self,
        *,
        timeout: float = SIGN_IN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = timeout
        self._clock = clock
        self._pending: dict[str, _Pending] = {}  # by server

    def __repr__(self) -> str:
        return f"SignIns(<{len(self._pending)} pending>)"

    def _cancel(self, server: str) -> None:
        pending = self._pending.pop(server, None)
        if pending is not None and pending.task is not None and not pending.task.done():
            pending.task.cancel()

    def _expire(self) -> None:
        now = self._clock()
        for server, pending in list(self._pending.items()):
            if now - pending.started > self._timeout:
                self._cancel(server)

    async def start(
        self,
        server: str,
        make_client: Callable[[WebSignIn], OAuthClient],
        redirect_uri: str,
        *,
        store: SecretsTokenStore,
        actor: dict[str, Any] | None = None,
    ) -> SignInStarted:
        """Start ``server``'s sign-in; any earlier pending one for it is cancelled."""
        self._expire()
        self._cancel(server)
        loop = asyncio.get_running_loop()
        pending = _Pending(
            nonce=secrets.token_urlsafe(32),
            server=server,
            actor=dict(actor or {}),
            started=self._clock(),
            redirect=loop.create_future(),
            callback=loop.create_future(),
        )
        oauth = make_client(WebSignIn(redirect_uri, pending, self._timeout))
        task = loop.create_task(oauth.access_token(interactive=True))
        pending.task = task
        self._pending[server] = pending
        task.add_done_callback(lambda t: self._finished(pending, store, t))
        waiting: set[asyncio.Future[Any]] = {task, pending.redirect}
        done, _ = await asyncio.wait(
            waiting, timeout=_START_SECONDS, return_when=asyncio.FIRST_COMPLETED
        )
        if pending.redirect in done and not task.done():
            return SignInStarted(
                "redirect", authorization_url=pending.redirect.result(), nonce=pending.nonce
            )
        if not done:
            self._cancel(server)
            store.last_failure = "sign_in_failed"
            return SignInStarted("failed", reason="sign_in_failed")
        self._pending.pop(server, None)
        if task.cancelled() or task.exception() is not None:
            exc = task.exception() if not task.cancelled() else asyncio.CancelledError()
            return SignInStarted("failed", reason=_reason(exc or RuntimeError()))
        return SignInStarted("signed_in")

    @staticmethod
    def _finished(pending: _Pending, store: SecretsTokenStore, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        store.last_failure = None if exc is None else _reason(exc)

    def _claim(self, nonce: str | None) -> _Pending | None:
        """The pending sign-in ``nonce`` names, removed so it is used once."""
        if not nonce:
            return None
        for server, pending in list(self._pending.items()):
            if hmac.compare_digest(pending.nonce.encode(), nonce.encode()):
                return self._pending.pop(server)
        return None

    async def complete(self, nonce: str | None, callback_url: str) -> Outcome:
        """Hand the callback ``callback_url`` to the sign-in ``nonce`` (the binding
        cookie) names, and wait for conduit's checks and the token exchange."""
        pending = self._claim(nonce)
        if pending is None:
            return Outcome(False, reason="unknown_sign_in")
        if self._clock() - pending.started > self._timeout:
            if pending.task is not None and not pending.task.done():
                pending.task.cancel()
            return Outcome(False, server=pending.server, reason="expired", actor=pending.actor)
        task = pending.task
        if task is None or task.done():
            return Outcome(False, server=pending.server, reason="expired", actor=pending.actor)
        if not pending.callback.done():
            pending.callback.set_result(callback_url)
        try:
            await asyncio.wait_for(asyncio.shield(task), _START_SECONDS)
        except Exception as exc:  # noqa: BLE001 - conduit's failure class is the answer
            return Outcome(False, server=pending.server, reason=_reason(exc), actor=pending.actor)
        return Outcome(True, server=pending.server, actor=pending.actor)

    def pending_servers(self) -> list[str]:
        self._expire()
        return sorted(self._pending)
