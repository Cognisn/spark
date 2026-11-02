"""Server-mode screens for getting a session without one: invitations, password reset, SSO.

SPARK renders each screen and calls warden's Python API on the server; the
browser never talks to warden's JSON routes.

* ``GET/POST /auth/invitation``: warden mails ``/auth/invitation?token=``.
  Accepting with a password activates the user with the staged roles and then
  signs them in, which goes on to the enrolment warden requires where it
  requires a second factor. Accepting through single sign-on starts the
  provider leg with the invitation sealed into it.
* ``GET/POST /auth/password-reset``: asks warden to mail a reset link. The
  answer is the same whatever the address, and is sent before warden does any
  work, so neither it nor its timing says whether the address has an account.
* ``GET/POST /auth/reset``: warden mails ``/auth/reset?token=``; completing it
  sets the password and ends every session of the user.
* ``GET /identity/auth/sso/start`` and ``/identity/auth/sso/callback``:
  single sign-on's browser leg, at warden's auth prefix (the callback is the
  redirect URI registered with the provider). The login binding cookie is
  scoped to that prefix. The callback hands the browser to
  ``/auth/sso/complete?code=`` with the same binding in a cookie scoped to the
  completion screen alone.
* ``GET /auth/sso/complete``: exchanges the handoff code on the server, with
  this browser's binding, and sets the session cookies; ``?error=sso_failed``
  (or any refusal) shows warden's fixed message.

Tokens and codes stay out of logs; screens that carry one are served with
``Referrer-Policy: no-referrer`` and ``Cache-Control: no-store``, and the
page removes the token from the address bar once read. A bad, used or expired
link gets one neutral message.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.background import BackgroundTask
from warden.identity import (
    SSO_FAILURE_MESSAGE,
    AuthenticationRefused,
    InvitationMismatch,
    LoginStart,
    SsoError,
    UserError,
)

from spark.identity.runtime import IdentityRuntime
from spark.identity.web import (
    INVITATION_PATH,
    RESET_PATH,
    RESET_REQUEST_PATH,
    SIGN_IN_PATH,
    SSO_BINDING_COOKIE,
    SSO_COMPLETE_PATH,
    SSO_HANDOFF_COOKIE,
    SSO_PREFIX,
    set_session_cookies,
    source_ip,
)
from spark.web.endpoints.identity import (
    forget_form_token,
    form_page,
    identity_runtime,
    private_headers,
    redirect,
    signed_in_or_challenged,
    sso_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter()

LINK_NOT_VALID = (
    "This link is not valid: it may have been used already or have expired. " "Ask for a new one."
)
RESET_REQUESTED = (
    "If that address has an account that signs in with a password, a link to choose a new "
    "password is on its way. It works once, for a limited time."
)
PASSWORDS_DIFFER = "The two passwords do not match."
USE_SSO = "This invitation is accepted through single sign-on."
SSO_START_PATH = SSO_PREFIX + "/start"
SSO_CALLBACK_PATH = SSO_PREFIX + "/callback"
_SSO_FAILED = f"{SSO_COMPLETE_PATH}?error=sso_failed"
_REDIRECT_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _min_length(runtime: IdentityRuntime) -> int:
    return int(runtime.config.identity.password_min_length)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# -- Invitations ----------------------------------------------------------------------------


async def _invitation_page(
    request: Request,
    runtime: IdentityRuntime,
    token: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return form_page(
        request,
        "auth_invitation.html",
        {
            "token": token,
            "error": error,
            "min_length": _min_length(runtime),
            "sso_enabled": await sso_enabled(runtime),
        },
        status_code=status_code,
    )


@router.get(INVITATION_PATH, response_class=HTMLResponse)
async def invitation_page(request: Request) -> HTMLResponse:
    """The screen an invitation link opens: choose a password, or single sign-on."""
    runtime = identity_runtime(request)
    token = request.query_params.get("token", "")
    if not token:
        return await _invitation_page(request, runtime, "", error=LINK_NOT_VALID, status_code=400)
    return await _invitation_page(request, runtime, token)


@router.post(INVITATION_PATH)
async def accept_invitation(request: Request) -> Response:
    """Accept with a password and sign in, or go to the provider to accept there."""
    runtime = identity_runtime(request)
    form = await request.form()
    token = _text(form.get("token"))
    if form.get("method") == "sso":
        return await _start_sso(request, runtime, invitation=token, status_code=303)
    password, confirm = _text(form.get("password")), _text(form.get("confirm"))
    if password != confirm:
        return await _invitation_page(
            request, runtime, token, error=PASSWORDS_DIFFER, status_code=400
        )
    ip = source_ip(runtime, request)
    try:
        accepted = await runtime.identity.invitations.accept_with_password(
            token, password, source_ip=ip
        )
    except UserError as exc:  # a password warden refuses; the link is untouched
        return await _invitation_page(request, runtime, token, error=str(exc), status_code=400)
    except InvitationMismatch:
        return await _invitation_page(request, runtime, token, error=USE_SSO, status_code=409)
    except AuthenticationRefused:
        return await _invitation_page(request, runtime, "", error=LINK_NOT_VALID, status_code=401)
    # Accepted: sign in with the new password, which goes on to any enrolment warden requires.
    try:
        result = await runtime.identity.sign_in(accepted.email, password, source_ip=ip)
    except AuthenticationRefused:
        response: Response = redirect(f"{SIGN_IN_PATH}?notice=accepted")
    else:
        response = signed_in_or_challenged(request, result.tokens, result.challenge, "/")
    forget_form_token(response, INVITATION_PATH)
    return response


# -- Password reset -------------------------------------------------------------------------


async def _request_reset(runtime: IdentityRuntime, email: str, ip: Any) -> None:
    """Ask warden for a reset link, after the answer has gone; never raises."""
    try:
        await runtime.identity.resets.request(email, source_ip=ip)
    except Exception as exc:  # audited by warden; the asker sees the same answer regardless
        logger.warning("A password reset request was not completed (%s)", type(exc).__name__)


def _reset_request_page(
    request: Request, *, requested: bool = False, status_code: int = 200
) -> HTMLResponse:
    return form_page(
        request,
        "auth_reset_request.html",
        {"notice": RESET_REQUESTED if requested else None, "error": None},
        status_code=status_code,
    )


@router.get(RESET_REQUEST_PATH, response_class=HTMLResponse)
async def reset_request_page(request: Request) -> HTMLResponse:
    identity_runtime(request)
    return _reset_request_page(request)


@router.post(RESET_REQUEST_PATH)
async def request_reset(request: Request) -> HTMLResponse:
    """Always the same answer; warden's work happens after it is sent."""
    runtime = identity_runtime(request)
    form = await request.form()
    response = _reset_request_page(request, requested=True)
    response.background = BackgroundTask(
        _request_reset, runtime, _text(form.get("email")), source_ip(runtime, request)
    )
    return response


def _reset_page(
    request: Request,
    runtime: IdentityRuntime,
    token: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return form_page(
        request,
        "auth_reset.html",
        {"token": token, "error": error, "min_length": _min_length(runtime)},
        status_code=status_code,
    )


@router.get(RESET_PATH, response_class=HTMLResponse)
async def reset_page(request: Request) -> HTMLResponse:
    """The screen a reset link opens: choose a new password."""
    runtime = identity_runtime(request)
    token = request.query_params.get("token", "")
    if not token:
        return _reset_page(request, runtime, "", error=LINK_NOT_VALID, status_code=400)
    return _reset_page(request, runtime, token)


@router.post(RESET_PATH)
async def complete_reset(request: Request) -> Response:
    """Set the new password (ending every session), then sign in again."""
    runtime = identity_runtime(request)
    form = await request.form()
    token = _text(form.get("token"))
    password, confirm = _text(form.get("password")), _text(form.get("confirm"))
    if password != confirm:
        return _reset_page(request, runtime, token, error=PASSWORDS_DIFFER, status_code=400)
    try:
        await runtime.identity.resets.complete(
            token, password, source_ip=source_ip(runtime, request)
        )
    except UserError as exc:  # the link is untouched
        return _reset_page(request, runtime, token, error=str(exc), status_code=400)
    except AuthenticationRefused:
        return _reset_page(request, runtime, "", error=LINK_NOT_VALID, status_code=401)
    response = redirect(f"{SIGN_IN_PATH}?notice=reset")
    forget_form_token(response, RESET_PATH)
    return response


# -- Single sign-on -------------------------------------------------------------------------


def _set_cookie(response: Response, name: str, value: str, path: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path=path, secure=True, httponly=True, samesite="lax"
    )


def _clear_cookie(response: Response, name: str, path: str) -> None:
    response.delete_cookie(name, path=path, secure=True, httponly=True, samesite="lax")


def _sso_failed() -> Response:
    response = Response(status_code=302, headers={"location": _SSO_FAILED, **_REDIRECT_HEADERS})
    _clear_cookie(response, SSO_BINDING_COOKIE, SSO_PREFIX)
    return response


async def _start_sso(
    request: Request, runtime: IdentityRuntime, *, invitation: str | None, status_code: int
) -> Response:
    """Begin a login (an invitation's, if given) and send the browser to the provider."""
    try:
        begun: LoginStart = await runtime.identity.sso.begin(
            source_ip=source_ip(runtime, request), invitation=invitation
        )
    except SsoError:
        return _sso_failed()
    response = Response(
        status_code=status_code, headers={"location": begun.redirect_url, **_REDIRECT_HEADERS}
    )
    _set_cookie(response, SSO_BINDING_COOKIE, begun.binding, SSO_PREFIX, begun.max_age)
    return response


@router.get(SSO_START_PATH)
async def sso_start(request: Request) -> Response:
    """A top-level navigation from the sign-in page: off to the provider."""
    runtime = identity_runtime(request)
    return await _start_sso(request, runtime, invitation=None, status_code=302)


@router.get(SSO_CALLBACK_PATH)
async def sso_callback(request: Request) -> Response:
    """The provider's redirect back: on to the completion screen with a handoff code."""
    runtime = identity_runtime(request)
    params = request.query_params
    binding = request.cookies.get(SSO_BINDING_COOKIE)
    try:
        handoff = await runtime.identity.sso.complete(
            state=params.get("state"),
            code=params.get("code"),
            error=params.get("error"),
            binding=binding,
            source_ip=source_ip(runtime, request),
            invitations=runtime.identity.invitations,
        )
    except SsoError:
        return _sso_failed()
    response = Response(
        status_code=302,
        headers={"location": f"{SSO_COMPLETE_PATH}?code={handoff}", **_REDIRECT_HEADERS},
    )
    _clear_cookie(response, SSO_BINDING_COOKIE, SSO_PREFIX)
    # The same binding, for the completion screen alone, for as long as the code lasts.
    _set_cookie(
        response,
        SSO_HANDOFF_COOKIE,
        str(binding),
        SSO_COMPLETE_PATH,
        int(runtime.config.sso.handoff_code_ttl_seconds),
    )
    return response


@router.get(SSO_COMPLETE_PATH, response_class=HTMLResponse)
async def sso_complete(request: Request) -> HTMLResponse:
    """Exchange the handoff code with this browser's binding; sign in or show the failure."""
    runtime = identity_runtime(request)
    code = request.query_params.get("code")
    pair = None
    if code and request.query_params.get("error") is None:
        try:
            pair = await runtime.identity.sso.exchange(
                code,
                request.cookies.get(SSO_HANDOFF_COOKIE),
                source_ip=source_ip(runtime, request),
            )
        except AuthenticationRefused:
            pair = None
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "auth_sso.html",
        {"signed_in": pair is not None, "error": None if pair else SSO_FAILURE_MESSAGE},
        status_code=200 if pair is not None or not code else 401,
    )
    private_headers(response)
    _clear_cookie(response, SSO_HANDOFF_COOKIE, SSO_COMPLETE_PATH)
    if pair is not None:
        set_session_cookies(response, pair)
    return response
