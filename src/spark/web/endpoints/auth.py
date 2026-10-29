"""Personal-mode sign-in and sign-out endpoints."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from spark.web.auth import (
    LOGIN_CSRF_COOKIE,
    LOGIN_PATH,
    SESSION_COOKIE,
    AuthManager,
    RedeemOutcome,
)
from spark.web.session import SessionManager

logger = logging.getLogger(__name__)

router = APIRouter()

_INVALID = (
    "Invalid or expired authentication code. Codes work once and expire after "
    "{minutes} minutes; reload this page and check the terminal for a new code."
)
_LOCKED = (
    "Too many failed attempts from this address. Try again in {minutes} minutes, "
    "using the code shown in the terminal."
)


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def _codes(request: Request) -> AuthManager:
    codes: AuthManager = request.app.state.auth
    return codes


def _sessions(request: Request) -> SessionManager:
    sessions: SessionManager = request.app.state.session
    return sessions


def _login_page(request: Request, error: str | None, status_code: int) -> HTMLResponse:
    """Render the sign-in form, re-using the browser's login token when it has one."""
    token = request.cookies.get(LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(32)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "csrf_token": token},
        status_code=status_code,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        path=LOGIN_PATH,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _signed_in(request: Request) -> RedirectResponse:
    """Start a new session and send the browser on to the loading page."""
    session_id = _sessions(request).create()
    response = RedirectResponse(url="/loading", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path=LOGIN_PATH)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the sign-in page, issuing a new code in the terminal if none is live."""
    codes = _codes(request)
    code = codes.ensure_code()
    if code is not None:
        logger.info(
            "Authentication code: %s (single use, expires in %d minutes)",
            code,
            _minutes(codes.policy.code_ttl_seconds),
        )
    return _login_page(request, None, 200)


@router.post(LOGIN_PATH)
async def authenticate(request: Request) -> Response:
    """Redeem the code from the sign-in form and create a session."""
    form = await request.form()
    code = str(form.get("code", ""))
    codes = _codes(request)
    outcome = codes.redeem(code, _client(request))
    if outcome is RedeemOutcome.LOCKED_OUT:
        minutes = _minutes(codes.policy.lockout_seconds)
        return _login_page(request, _LOCKED.format(minutes=minutes), 429)
    if outcome is RedeemOutcome.REFUSED:
        minutes = _minutes(codes.policy.code_ttl_seconds)
        return _login_page(request, _INVALID.format(minutes=minutes), 401)
    return _signed_in(request)


@router.get("/auto-login")
async def auto_login(request: Request) -> RedirectResponse:
    """Redeem the code in the URL opened at start-up (once only)."""
    code = request.query_params.get("code", "")
    if _codes(request).redeem(code, _client(request)) is not RedeemOutcome.ACCEPTED:
        return RedirectResponse(url="/login", status_code=303)
    return _signed_in(request)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """End this browser's session and return to the sign-in page."""
    _sessions(request).destroy(request.cookies.get(SESSION_COOKIE) or "")
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict")
    return response
