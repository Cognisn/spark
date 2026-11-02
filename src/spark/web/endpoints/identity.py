"""Server-mode sign-in screens and session endpoints (warden).

* ``GET/POST /auth/sign-in``: the sign-in form; a password sign-in through
  warden either sets the session cookies or hands a second-factor challenge to
  ``/auth/challenge``.
* ``GET /auth/challenge``: where a challenge lands. A placeholder until the
  TOTP and enrolment screens (step 5) complete it with
  ``identity.verify_totp``, ``use_recovery_code``, ``begin_enrolment`` and
  ``confirm_enrolment``, then :func:`spark.identity.web.set_session_cookies`.
* ``GET/POST /auth/session/refresh``: rotates both cookies. The GET serves
  page loads (redirected here by the auth middleware, then back to ``next``);
  the POST serves ``csrf.js`` and is Origin and CSRF checked by the guard.
* ``POST /auth/sign-out`` (and the menu's ``POST /logout``): revokes the warden
  session and clears both cookies.
* ``GET /auth/session``: who is signed in (and the session's CSRF token).

In personal mode every route here answers 404: personal mode signs in with
its terminal code (:mod:`spark.web.endpoints.auth`).
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from warden.identity import AuthenticationRefused, ChallengeIssued, Principal

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
    SIGN_IN_CSRF_COOKIE,
    SIGN_IN_PATH,
    SIGN_OUT_PATH,
    WardenAuthenticator,
    clear_session_cookies,
    safe_next,
    set_session_cookies,
    signed_claims,
    source_ip,
    via_api_key,
)
from spark.web.security import ORIGIN_REFUSED

logger = logging.getLogger(__name__)

router = APIRouter()

SIGN_IN_FAILED = "Sign-in failed. Check your email address and password and try again."
_NO_STORE = {"Cache-Control": "no-store"}


def _runtime(request: Request) -> IdentityRuntime:
    """warden's runtime; 404 in personal mode (or before start-up has bound it)."""
    authenticator = getattr(request.app.state, "authenticator", None)
    runtime = authenticator.runtime if isinstance(authenticator, WardenAuthenticator) else None
    if runtime is None:
        raise HTTPException(status_code=404)
    return runtime


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers=_NO_STORE)


def _with_next(path: str, next_path: str) -> str:
    return path if next_path == "/" else f"{path}?{urlencode({'next': next_path})}"


# -- Sign-in --------------------------------------------------------------------------------


def _sign_in_page(
    request: Request, *, error: str | None, status_code: int, next_path: str
) -> HTMLResponse:
    """The sign-in form, re-using the browser's form token when it has one."""
    token = request.cookies.get(SIGN_IN_CSRF_COOKIE) or secrets.token_urlsafe(32)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {
            "variant": "server",
            "error": error,
            "csrf_token": token,
            "next_path": next_path,
            "form_action": SIGN_IN_PATH,
        },
        status_code=status_code,
    )
    response.set_cookie(
        SIGN_IN_CSRF_COOKIE,
        token,
        path=SIGN_IN_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers.update(_NO_STORE)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get(SIGN_IN_PATH, response_class=HTMLResponse)
async def sign_in_page(request: Request) -> HTMLResponse:
    _runtime(request)
    return _sign_in_page(
        request,
        error=None,
        status_code=200,
        next_path=safe_next(request.query_params.get("next")),
    )


@router.post(SIGN_IN_PATH)
async def sign_in(request: Request) -> Response:
    """Check the password through warden; sign in, or hand over a second-factor challenge."""
    runtime = _runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    try:
        result = await runtime.identity.sign_in(
            form.get("email"), form.get("password"), source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        # One answer for every refusal; warden's audit trail records which it was.
        return _sign_in_page(request, error=SIGN_IN_FAILED, status_code=401, next_path=next_path)
    if result.challenge is not None:
        return challenge_issued(request, result.challenge, next_path)
    if result.tokens is None:  # warden returns exactly one of the two
        raise HTTPException(status_code=500)
    response = _redirect(next_path)
    set_session_cookies(response, result.tokens)
    response.delete_cookie(SIGN_IN_CSRF_COOKIE, path=SIGN_IN_PATH, secure=True, httponly=True)
    return response


def challenge_issued(request: Request, challenge: ChallengeIssued, next_path: str) -> Response:
    """Keep a second-factor challenge in a cookie scoped to the challenge screens.

    The cookie holds ``<purpose>:<token>`` (purpose ``verify`` or ``enrol``),
    so the token never appears in a URL. Step 5's screens read it with
    :func:`challenge_of`.
    """
    response = _redirect(_with_next(CHALLENGE_PATH, next_path))
    response.set_cookie(
        CHALLENGE_COOKIE,
        f"{challenge.purpose}:{challenge.token}",
        max_age=challenge.expires_in,
        path=CHALLENGE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(SIGN_IN_CSRF_COOKIE, path=SIGN_IN_PATH, secure=True, httponly=True)
    return response


def challenge_of(request: Request) -> tuple[str, str] | None:
    """The pending challenge's purpose and token, or None."""
    value = request.cookies.get(CHALLENGE_COOKIE, "")
    purpose, _, token = value.partition(":")
    return (purpose, token) if purpose in ("verify", "enrol") and token else None


@router.get(CHALLENGE_PATH, response_class=HTMLResponse)
async def challenge_page(request: Request) -> Response:
    """Placeholder: the second-factor and enrolment screens arrive in step 5."""
    _runtime(request)
    pending = challenge_of(request)
    if pending is None:
        return _redirect(SIGN_IN_PATH)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {"variant": "challenge", "purpose": pending[0], "error": None},
    )
    response.headers.update(_NO_STORE)
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
    runtime = _runtime(request)
    site = request.headers.get("sec-fetch-site", "").lower()
    if site in ("cross-site", "same-site"):
        return HTMLResponse(ORIGIN_REFUSED, status_code=403, headers=_NO_STORE)
    next_path = safe_next(request.query_params.get("next"))
    pair = await _rotate(request, runtime)
    if pair is None:
        response = _redirect(_with_next(SIGN_IN_PATH, next_path))
        clear_session_cookies(response)
        return response
    response = _redirect(next_path)
    set_session_cookies(response, pair)
    return response


@router.post(REFRESH_PATH)
async def refresh(request: Request) -> Response:
    """``csrf.js``'s refresh: 204 with new cookies, or 401 asking for sign-in."""
    runtime = _runtime(request)
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
    runtime = _runtime(request)
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
    response = _redirect(SIGN_IN_PATH)
    clear_session_cookies(response)
    return response


@router.post(SIGN_OUT_PATH)
async def sign_out(request: Request) -> Response:
    return await sign_out_response(request)


@router.get(SESSION_PATH)
async def session(request: Request) -> JSONResponse:
    """Who is signed in: for ``csrf.js`` (a cheap session check) and step 5's screens."""
    _runtime(request)
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
