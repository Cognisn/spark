"""Server-mode sign-in screens and session endpoints (warden).

* ``GET/POST /auth/sign-in``: the sign-in form; a password sign-in through
  warden either sets the session cookies or hands a second-factor challenge to
  ``/auth/challenge``.
* ``GET /auth/challenge``: the second factor. A ``verify`` challenge asks for a
  TOTP code or a recovery code (``POST /auth/challenge/verify``); an ``enrol``
  challenge (warden requires a second factor the user has not set up) starts
  an enrolment and shows its QR code and key, and ``POST /auth/challenge/enrol``
  confirms it, signs the user in and shows the recovery codes, once.
* ``GET/POST /auth/session/refresh``: rotates both cookies. The GET serves
  page loads (redirected here by the auth middleware, then back to ``next``);
  the POST serves ``csrf.js`` and is Origin and CSRF checked by the guard.
* ``POST /auth/sign-out`` (and the menu's ``POST /logout``): revokes the warden
  session and clears both cookies.
* ``GET /auth/session``: who is signed in (and the session's CSRF token).

The challenge's token lives only in its cookie, scoped to ``/auth/challenge``;
the forms there carry a CSRF token bound to it, so a page from one browser's
challenge never completes another's. Every refusal of a code is one message,
never which factor failed. Invitations, password resets and single sign-on are
in :mod:`spark.web.endpoints.lifecycle`.

In personal mode every route here answers 404: personal mode signs in with
its terminal code (:mod:`spark.web.endpoints.auth`).
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from warden.identity import AuthenticationRefused, ChallengeIssued, Principal, TokenPair

from spark.identity.access import current_identity
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import (
    ACCESS_COOKIE,
    CHALLENGE_COOKIE,
    CHALLENGE_PATH,
    REFRESH_COOKIE,
    REFRESH_PATH,
    SESSION_HEADER,
    SESSION_PATH,
    SIGN_IN_PATH,
    SIGN_OUT_PATH,
    WardenAuthenticator,
    clear_session_cookies,
    form_csrf_cookie,
    safe_next,
    set_session_cookies,
    signed_claims,
    source_ip,
    via_api_key,
)
from spark.web.qr import qr_data_uri
from spark.web.security import ORIGIN_REFUSED

logger = logging.getLogger(__name__)

router = APIRouter()

SIGN_IN_FAILED = "Sign-in failed. Check your email address and password and try again."
VERIFICATION_FAILED = (
    "Verification failed. Check the code and try again; if it keeps failing, start again."
)
CHALLENGE_VERIFY_PATH = CHALLENGE_PATH + "/verify"
CHALLENGE_ENROL_PATH = CHALLENGE_PATH + "/enrol"
# Fixed notices the sign-in page may show (?notice=...); nothing from the URL is echoed.
NOTICES = {
    "accepted": "Your invitation is accepted. Sign in to continue.",
    "reset": "Your password is changed. Sign in with your new password.",
}
_NO_STORE = {"Cache-Control": "no-store"}
_BASE32 = re.compile(r"[A-Z2-7]{16,128}")


def identity_runtime(request: Request) -> IdentityRuntime:
    """warden's runtime; 404 in personal mode (or before start-up has bound it)."""
    authenticator = getattr(request.app.state, "authenticator", None)
    runtime = authenticator.runtime if isinstance(authenticator, WardenAuthenticator) else None
    if runtime is None:
        raise HTTPException(status_code=404)
    return runtime


def redirect(url: str) -> RedirectResponse:
    """A 303 that is never cached."""
    return RedirectResponse(url, status_code=303, headers=_NO_STORE)


def with_next(path: str, next_path: str) -> str:
    """``path``, carrying a same-origin ``next`` unless it is ``/``."""
    return path if next_path == "/" else f"{path}?{urlencode({'next': next_path})}"


def private_headers(response: Response) -> None:
    """A screen that may show a secret: never cached, never sent on as a referrer."""
    response.headers.update(_NO_STORE)
    response.headers["Referrer-Policy"] = "no-referrer"


def form_page(
    request: Request, template: str, context: dict[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    """A page used before a session exists, with its own form token.

    The token is kept in a cookie scoped to this page's path
    (:func:`spark.identity.web.form_csrf_cookie`), re-using the browser's
    when it has one, and the page's form posts back to the same path.
    """
    path = request.url.path
    cookie = form_csrf_cookie(path)
    if cookie is None:
        raise RuntimeError(f"{path} has no form token cookie")
    token = request.cookies.get(cookie) or secrets.token_urlsafe(32)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, template, {**context, "csrf_token": token}, status_code=status_code
    )
    response.set_cookie(cookie, token, path=path, secure=True, httponly=True, samesite="strict")
    private_headers(response)
    return response


def forget_form_token(response: Response, path: str) -> None:
    """Remove the form token cookie of ``path`` once its form has done its work."""
    cookie = form_csrf_cookie(path)
    if cookie is not None:
        response.delete_cookie(cookie, path=path, secure=True, httponly=True)


async def sso_enabled(runtime: IdentityRuntime) -> bool:
    """Whether single sign-on is switched on and configured (for its buttons)."""
    try:
        settings = await runtime.identity.sso.settings.current()
    except Exception:  # a settings read failing must not break sign-in
        logger.warning("Could not read the single sign-on settings")
        return False
    return bool(settings.enabled and settings.issuer_url and settings.client_id)


def signed_in_response(target: str, pair: TokenPair) -> RedirectResponse:
    """Hand a new session to the browser and go to ``target`` (already a safe path)."""
    response = redirect(target)
    set_session_cookies(response, pair)
    return response


# -- Sign-in --------------------------------------------------------------------------------


async def sign_in_page(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    next_path: str = "/",
    notice: str | None = None,
) -> HTMLResponse:
    """The sign-in form, re-using the browser's form token when it has one."""
    runtime = identity_runtime(request)
    return form_page(
        request,
        "auth_sign_in.html",
        {
            "error": error,
            "notice": notice,
            "next_path": next_path,
            "sso_enabled": await sso_enabled(runtime),
        },
        status_code=status_code,
    )


@router.get(SIGN_IN_PATH, response_class=HTMLResponse)
async def sign_in_get(request: Request) -> HTMLResponse:
    identity_runtime(request)
    return await sign_in_page(
        request,
        next_path=safe_next(request.query_params.get("next")),
        notice=NOTICES.get(request.query_params.get("notice", "")),
    )


@router.post(SIGN_IN_PATH)
async def sign_in(request: Request) -> Response:
    """Check the password through warden; sign in, or hand over a second-factor challenge."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    try:
        result = await runtime.identity.sign_in(
            form.get("email"), form.get("password"), source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        # One answer for every refusal; warden's audit trail records which it was.
        return await sign_in_page(
            request, error=SIGN_IN_FAILED, status_code=401, next_path=next_path
        )
    response = signed_in_or_challenged(request, result.tokens, result.challenge, next_path)
    forget_form_token(response, SIGN_IN_PATH)
    return response


def signed_in_or_challenged(
    request: Request,
    tokens: TokenPair | None,
    challenge: ChallengeIssued | None,
    next_path: str,
) -> Response:
    """The answer to a right password: a session, or the challenge screen."""
    if challenge is not None:
        return challenge_issued(request, challenge, next_path)
    if tokens is None:  # warden returns exactly one of the two
        raise HTTPException(status_code=500)
    return signed_in_response(next_path, tokens)


def challenge_issued(request: Request, challenge: ChallengeIssued, next_path: str) -> Response:
    """Keep a second-factor challenge in a cookie scoped to the challenge screens.

    The cookie holds ``<purpose>:<token>`` (purpose ``verify`` or ``enrol``),
    so the token never appears in a URL; :func:`challenge_of` reads it.
    """
    response = redirect(with_next(CHALLENGE_PATH, next_path))
    response.set_cookie(
        CHALLENGE_COOKIE,
        f"{challenge.purpose}:{challenge.token}",
        max_age=challenge.expires_in,
        path=CHALLENGE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


def challenge_of(request: Request) -> tuple[str, str] | None:
    """The pending challenge's purpose and token, or None.

    Read from this browser's challenge cookie only, never from a form or a URL.
    """
    value = request.cookies.get(CHALLENGE_COOKIE, "")
    purpose, _, token = value.partition(":")
    return (purpose, token) if purpose in ("verify", "enrol") and token else None


def _clear_challenge(response: Response) -> None:
    response.delete_cookie(CHALLENGE_COOKIE, path=CHALLENGE_PATH, secure=True, httponly=True)


def _challenge_page(
    request: Request,
    purpose: str,
    next_path: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    secret: str = "",
    uri: str = "",
) -> HTMLResponse:
    authenticator: WardenAuthenticator = request.app.state.authenticator
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "auth_challenge.html",
        {
            "purpose": purpose,
            "error": error,
            "next_path": next_path,
            "csrf_token": authenticator.csrf_token(request) or "",
            "secret": secret,
            "uri": uri,
            "qr": qr_data_uri(uri) if uri else None,
        },
        status_code=status_code,
    )
    private_headers(response)
    return response


def _start_over(next_path: str) -> Response:
    """Back to sign-in, the spent or unknown challenge forgotten."""
    response = redirect(with_next(SIGN_IN_PATH, next_path))
    _clear_challenge(response)
    return response


@router.get(CHALLENGE_PATH, response_class=HTMLResponse)
async def challenge_page(request: Request) -> Response:
    """Ask for the second factor, or start the enrolment warden requires."""
    runtime = identity_runtime(request)
    next_path = safe_next(request.query_params.get("next"))
    pending = challenge_of(request)
    if pending is None:
        return redirect(with_next(SIGN_IN_PATH, next_path))
    purpose, token = pending
    if purpose == "verify":
        return _challenge_page(request, purpose, next_path)
    try:
        enrolment = await runtime.identity.begin_enrolment(
            challenge=token, source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        return _start_over(next_path)
    return _challenge_page(
        request, purpose, next_path, secret=enrolment.secret_base32, uri=enrolment.uri
    )


@router.post(CHALLENGE_VERIFY_PATH)
async def challenge_verify(request: Request) -> Response:
    """Finish signing in with a TOTP code or a recovery code."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    pending = challenge_of(request)
    if pending is None or pending[0] != "verify":
        return _start_over(next_path)
    verify = (
        runtime.identity.use_recovery_code
        if form.get("kind") == "recovery"
        else runtime.identity.verify_totp
    )
    try:
        pair = await verify(pending[1], form.get("code"), source_ip=source_ip(runtime, request))
    except AuthenticationRefused:
        return _challenge_page(
            request, "verify", next_path, error=VERIFICATION_FAILED, status_code=401
        )
    response = signed_in_response(next_path, pair)
    _clear_challenge(response)
    return response


def shown_back(form: Any) -> tuple[str, str]:
    """An enrolment's key and URI a page posted back, for showing again only.

    They are never used to verify anything (warden holds the secret); only a
    well-formed key and an ``otpauth://totp/`` URI are shown.
    """
    secret, uri = str(form.get("secret") or ""), str(form.get("uri") or "")
    if _BASE32.fullmatch(secret) is None or not uri.startswith("otpauth://totp/"):
        return "", ""
    return secret, uri


@router.post(CHALLENGE_ENROL_PATH)
async def challenge_enrol(request: Request) -> Response:
    """Confirm the enrolment, sign in, and show the recovery codes once."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    pending = challenge_of(request)
    if pending is None or pending[0] != "enrol":
        return _start_over(next_path)
    try:
        codes, pair = await runtime.identity.confirm_enrolment(
            form.get("code"), challenge=pending[1], source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        secret, uri = shown_back(form)
        return _challenge_page(
            request,
            "enrol",
            next_path,
            error=VERIFICATION_FAILED,
            status_code=401,
            secret=secret,
            uri=uri,
        )
    if pair is None:  # under a challenge warden always signs in
        raise HTTPException(status_code=500)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "auth_recovery_codes.html",
        {"recovery_codes": codes, "next_path": next_path, "error": None},
    )
    # Shown once: never cached, so neither Back nor the history shows them again.
    private_headers(response)
    set_session_cookies(response, pair)
    _clear_challenge(response)
    return response


# -- Refresh ---------------------------------------------------------------------------------


async def _rotate(request: Request, runtime: IdentityRuntime) -> Any:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        return None
    try:
        return await runtime.identity.refresh(token, source_ip=source_ip(runtime, request))
    except AuthenticationRefused:
        return None


@router.get(REFRESH_PATH)
async def refresh_navigation(request: Request) -> Response:
    """A page load's refresh: rotate both cookies and go back to ``next``.

    Only a same-origin (or typed) navigation may refresh: the refresh cookie is
    SameSite=Strict, and fetch metadata naming another site is refused.
    """
    runtime = identity_runtime(request)
    site = request.headers.get("sec-fetch-site", "").lower()
    if site in ("cross-site", "same-site"):
        return HTMLResponse(ORIGIN_REFUSED, status_code=403, headers=_NO_STORE)
    next_path = safe_next(request.query_params.get("next"))
    pair = await _rotate(request, runtime)
    if pair is None:
        response = redirect(with_next(SIGN_IN_PATH, next_path))
        clear_session_cookies(response)
        return response
    response = redirect(next_path)
    set_session_cookies(response, pair)
    return response


@router.post(REFRESH_PATH)
async def refresh(request: Request) -> Response:
    """``csrf.js``'s refresh: 204 with new cookies, or 401 asking for sign-in."""
    runtime = identity_runtime(request)
    pair = await _rotate(request, runtime)
    if pair is None:
        refused = JSONResponse(
            {"detail": "not signed in"},
            status_code=401,
            headers={SESSION_HEADER: "sign-in", **_NO_STORE},
        )
        clear_session_cookies(refused)
        return refused
    response = Response(status_code=204)
    set_session_cookies(response, pair)
    return response


# -- Sign-out and who am I --------------------------------------------------------------------


async def sign_out_response(request: Request) -> Response:
    """Revoke the warden session the access cookie names, clear both cookies, go to sign-in.

    The token's signature is checked but not its expiry, so an expired session
    can still be ended. The guard has already checked Origin and the CSRF token
    bound to that same session.
    """
    runtime = identity_runtime(request)
    token = request.cookies.get(ACCESS_COOKIE)
    claims = await signed_claims(runtime, token) if token else None
    if claims is not None:
        principal = Principal(
            user_id=claims.user_id,
            session_id=claims.session_id,
            email="",
            roles=frozenset(),
            permissions=frozenset(),
            amr=claims.amr,
        )
        await runtime.identity.sign_out(principal, source_ip=source_ip(runtime, request))
    response = redirect(SIGN_IN_PATH)
    clear_session_cookies(response)
    return response


@router.post(SIGN_OUT_PATH)
async def sign_out(request: Request) -> Response:
    return await sign_out_response(request)


@router.get(SESSION_PATH)
async def session(request: Request) -> JSONResponse:
    """Who is signed in: for ``csrf.js`` (a cheap session check) and step 5's screens."""
    identity_runtime(request)
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401)
    authenticator: WardenAuthenticator = request.app.state.authenticator
    return JSONResponse(
        {
            "email": identity.email,
            "user_guid": identity.user_guid,
            "warden_user_id": identity.warden_user_id,
            "roles": sorted(identity.roles),
            "permissions": sorted(identity.permissions),
            "via": "api_key" if via_api_key(request) else "session",
            "csrf_token": authenticator.csrf_token(request),
        },
        headers=_NO_STORE,
    )
