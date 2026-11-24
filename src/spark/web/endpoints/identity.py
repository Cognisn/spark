"""Server-mode sign-in screens and session endpoints (warden).

* ``GET/POST /auth/sign-in``: the sign-in form. From warden 0.2.0 a right
  password never sets the session cookies by itself: warden always answers with
  a second-factor challenge (ruling CS14), handed to ``/auth/challenge``.
* ``GET /auth/challenge``: the second factor. A ``verify`` challenge asks for a
  TOTP code or a recovery code (``POST /auth/challenge/verify``); an ``enrol``
  challenge (a user holding no allowed second factor: never enrolled, or only
  a kind the administrator no longer allows) starts an enrolment of one of the
  kinds warden names and shows its QR code and key, and
  ``POST /auth/challenge/enrol`` confirms it, signs the user in and shows the
  recovery codes warden returns, once. The kinds warden names (``methods``)
  travel in the challenge cookie; this screen offers TOTP only when warden
  names it, and a security key or passkey (``webauthn``) only when warden
  names that: ``POST /auth/challenge/webauthn/options`` begins the ceremony
  (verifying with a key the user holds, or registering one for an
  enrolment) and ``POST /auth/challenge/webauthn`` finishes it with the
  browser's answer.
* A ``change`` challenge: warden requires a new password before it issues the
  session (an administrator asked, the password expired, no longer meets the
  policy or is on a watch list). It follows a right password and any second
  factor, in place of the session; ``POST /auth/challenge/change`` sets the new
  password through warden and signs in. After a forced enrolment the recovery
  codes are still shown first, without a session, and lead here.
* ``POST /auth/sign-in/passkey/options`` and ``POST /auth/sign-in/passkey``:
  signing in with a passkey alone (no password, no second step), offered
  only while warden can use passkeys (its capabilities). warden decides who
  may: ``passkey_only`` users always, others only while passkeys are an
  allowed second factor. Every refusal is one message.
* ``GET/POST /auth/passkey-registration`` and its ``/options``: the screen an
  administrator's emailed registration link opens; it registers a passkey
  and signs nobody in.
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

A passkey ceremony is two requests from ``static/js/webauthn.js``: a fetch
for the options (JSON, the page's CSRF token in its header), then the page's
own form posted with the browser's answer in its ``credential`` field, so
every outcome is an ordinary page (the session cookies, recovery codes shown
once, or the screen's fixed message). The answer is passed to warden as it
came; warden checks it and never says why it refused one.

In personal mode every route here answers 404: personal mode signs in with
its terminal code (:mod:`spark.web.endpoints.auth`).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from warden.identity import (
    AuthenticationRefused,
    ChallengeIssued,
    PasswordChangeIssued,
    PasswordRejected,
    Principal,
    SignInResult,
    TokenPair,
    WebAuthnEnrolled,
    WebAuthnError,
    WebAuthnOptions,
    WebAuthnUnavailable,
)

from spark.identity import sso
from spark.identity.access import current_identity
from spark.identity.passwords import PASSWORDS_DIFFER, change_reason_text, refusal_text
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import (
    ACCESS_COOKIE,
    CHALLENGE_COOKIE,
    CHALLENGE_PATH,
    PASSKEY_REGISTRATION_OPTIONS_PATH,
    PASSKEY_REGISTRATION_PATH,
    PASSKEY_SIGN_IN_OPTIONS_PATH,
    PASSKEY_SIGN_IN_PATH,
    REFRESH_COOKIE,
    REFRESH_PATH,
    SESSION_HEADER,
    SESSION_PATH,
    SIGN_IN_PATH,
    SIGN_OUT_PATH,
    WardenAuthenticator,
    clear_session_cookies,
    form_csrf_cookie,
    form_path,
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
CHALLENGE_CHANGE_PATH = CHALLENGE_PATH + "/change"
CHALLENGE_WEBAUTHN_PATH = CHALLENGE_PATH + "/webauthn"
CHALLENGE_WEBAUTHN_OPTIONS_PATH = CHALLENGE_WEBAUTHN_PATH + "/options"
# One message for every refused passkey sign-in: warden's audit trail says which it was.
PASSKEY_SIGN_IN_FAILED = (
    "Sign-in with a passkey did not work. Try again, or use another way to sign in."
)
LINK_NOT_VALID = (
    "This link is not valid: it may have been used already or have expired. Ask for a new one."
)
KEY_NOT_REGISTERED = (
    "The passkey could not be registered. Try again, or use another passkey or security key."
)
KEY_LIMIT = (
    "This account already has as many passkeys and security keys as it may. "
    "Remove one before adding another."
)
# The largest browser answer passed on to warden (a WebAuthn response is a few KiB).
MAX_CREDENTIAL_BYTES = 64 * 1024
_PURPOSES = ("verify", "enrol", "change")
# The second-factor kinds a challenge may name (warden's SecondFactor values).
_KINDS = ("totp", "webauthn")
NO_ENROLMENT = (
    "Your account needs a second factor that this page cannot set up. "
    "Ask an administrator to help you sign in."
)
NO_TOTP = (
    "Your account has no second factor that can be used here. "
    "Use a recovery code, or ask an administrator to help you sign in."
)
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
    path = form_path(request.url.path)
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
    """Whether single sign-on is switched on and configured, client secret included.

    Every SSO button, wording and sign-in method choice follows this
    (:func:`spark.identity.sso.available`); a failed read offers none.
    """
    return await sso.available(runtime.identity)


async def passkeys_available(runtime: IdentityRuntime) -> bool:
    """Whether warden can use passkeys and security keys here (its capabilities).

    Only where a relying party is known: ``server.public_base_url`` names a
    host, not an IP address (:func:`spark.identity.runtime.check_webauthn`).
    """
    return (await runtime.identity.capabilities()).webauthn


def credential_of(form: Any) -> dict[str, Any] | None:
    """The browser's WebAuthn answer a form posted (``credential``, JSON), or None.

    Passed to warden unchanged, which checks every part of it; anything that
    is not a JSON object of a sensible size is None, which warden refuses.
    """
    text = form.get("credential")
    if not isinstance(text, str) or len(text) > MAX_CREDENTIAL_BYTES:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def nickname_of(form: Any) -> str | None:
    """A key's name from a form: trimmed, or None when blank (warden checks the rest)."""
    value = form.get("nickname")
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def options_response(options: WebAuthnOptions) -> JSONResponse:
    """The options for ``navigator.credentials``, as ``webauthn.js`` reads them; never cached."""
    return JSONResponse({"publicKey": options.public_key}, headers=_NO_STORE)


def options_refused(detail: str, status_code: int = 401) -> JSONResponse:
    """A refused options request: one fixed message for the page to show."""
    return JSONResponse({"detail": detail}, status_code=status_code, headers=_NO_STORE)


async def json_body(request: Request) -> dict[str, Any]:
    """An options request's JSON object, or an empty one."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


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
            "passkey_sign_in": await passkeys_available(runtime),
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
    response = signed_in_or_challenged(request, result, next_path)
    forget_form_token(response, SIGN_IN_PATH)
    return response


def signed_in_or_challenged(request: Request, result: SignInResult, next_path: str) -> Response:
    """The answer to a right password: a session, a second-factor challenge, or a change."""
    if result.challenge is not None:
        return challenge_issued(request, result.challenge, next_path)
    if result.password_change is not None:
        return change_required(result.password_change, next_path)
    if result.tokens is None:  # warden returns exactly one of the three
        raise HTTPException(status_code=500)
    return signed_in_response(next_path, result.tokens)


def session_or_change(next_path: str, outcome: TokenPair | PasswordChangeIssued) -> Response:
    """After a second factor: the session, or the password change warden requires first."""
    if isinstance(outcome, PasswordChangeIssued):
        return change_required(outcome, next_path)
    response = signed_in_response(next_path, outcome)
    _clear_challenge(response)
    return response


def _set_challenge(response: Response, purpose: str, token: str, expires_in: int) -> None:
    """Keep a challenge in a cookie scoped to the challenge screens, replacing any other.

    The cookie holds ``<purpose>:<token>`` (purpose ``verify``, ``enrol`` or
    ``change``), so the token never appears in a URL; :func:`challenge_of`
    reads it.
    """
    response.set_cookie(
        CHALLENGE_COOKIE,
        f"{purpose}:{token}",
        max_age=expires_in,
        path=CHALLENGE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def challenge_issued(request: Request, challenge: ChallengeIssued, next_path: str) -> Response:
    """Go to the second-factor screen, its challenge in this browser's cookie.

    The kinds warden names (``methods``: for ``verify`` the allowed kinds the
    user holds, for ``enrol`` the kinds they may enrol) ride in the cookie
    beside the purpose, so the screen offers only those.
    """
    response = redirect(with_next(CHALLENGE_PATH, next_path))
    # "+" between kinds: a cookie-safe character, so the value is never quoted.
    methods = "+".join(str(m) for m in challenge.methods if str(m) in _KINDS)
    _set_challenge(
        response, f"{challenge.purpose}:{methods}", challenge.token, challenge.expires_in
    )
    return response


def change_path(issued: PasswordChangeIssued, next_path: str) -> str:
    """The change screen, naming warden's reason (only ever shown as a fixed message)."""
    query = {"reason": str(issued.reason)}
    if next_path != "/":
        query["next"] = next_path
    return f"{CHALLENGE_PATH}?{urlencode(query)}"


def change_required(issued: PasswordChangeIssued, next_path: str) -> Response:
    """Go to the change screen; no session exists until the new password is set."""
    response = redirect(change_path(issued, next_path))
    _set_challenge(response, "change", issued.token, issued.expires_in)
    return response


def challenge_of(request: Request) -> tuple[str, str] | None:
    """The pending challenge's purpose and token, or None.

    Read from this browser's challenge cookie only, never from a form or a URL.
    """
    found = _challenge_cookie(request)
    return (found[0], found[2]) if found is not None else None


def challenge_methods(request: Request) -> tuple[str, ...]:
    """The second-factor kinds warden named for the pending challenge (none for a change)."""
    found = _challenge_cookie(request)
    return found[1] if found is not None else ()


def _challenge_cookie(request: Request) -> tuple[str, tuple[str, ...], str] | None:
    """``<purpose>:<token>``, or ``<purpose>:<kinds>:<token>`` for a second factor.

    A second-factor cookie without kinds was set before warden 0.2.0 (a sign-in
    in flight across the upgrade), when TOTP was the only kind: it keeps TOTP.
    """
    parts = request.cookies.get(CHALLENGE_COOKIE, "").split(":")
    if len(parts) == 2:
        purpose, token = parts
        kinds = "" if purpose == "change" else "totp"
    elif len(parts) == 3:
        purpose, kinds, token = parts
    else:
        return None
    if purpose not in _PURPOSES or not token:
        return None
    return purpose, tuple(k for k in kinds.split("+") if k in _KINDS), token


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
    reason: str = "",
    notice: str | None = None,
) -> HTMLResponse:
    authenticator: WardenAuthenticator = request.app.state.authenticator
    methods = challenge_methods(request)
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
            # For a change: warden's reason, as one of the fixed messages only.
            "reason": reason,
            "reason_text": change_reason_text(reason),
            "min_length": _min_length(request),
            # Only the kinds warden named: TOTP is asked for, or enrolled, only then.
            "totp": "totp" in methods,
            # A security key or passkey, likewise (_passkey_challenge.html).
            "webauthn": "webauthn" in methods,
            "factor_notice": notice,
        },
        status_code=status_code,
    )
    private_headers(response)
    return response


def _min_length(request: Request) -> int:
    """warden's minimum password length, for the change form's hint."""
    runtime = identity_runtime(request)
    return int(runtime.config.identity.password_min_length)


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
    methods = challenge_methods(request)
    if purpose == "verify":
        notice = None if methods else NO_TOTP
        return _challenge_page(request, purpose, next_path, notice=notice)
    if purpose == "change":
        reason = request.query_params.get("reason", "")
        return _challenge_page(request, purpose, next_path, reason=reason)
    if not methods:
        # warden names no kind this screen can enrol: nothing is started, and
        # nothing signs in.
        return _challenge_page(request, purpose, next_path, notice=NO_ENROLMENT)
    if "totp" not in methods:
        # Only a security key or passkey: its ceremony starts from the page.
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
    recovery = form.get("kind") == "recovery"
    if not recovery and "totp" not in challenge_methods(request):
        return _challenge_page(
            request, "verify", next_path, error=VERIFICATION_FAILED, status_code=401
        )
    verify = runtime.identity.use_recovery_code if recovery else runtime.identity.verify_totp
    try:
        outcome = await verify(pending[1], form.get("code"), source_ip=source_ip(runtime, request))
    except AuthenticationRefused:
        return _challenge_page(
            request, "verify", next_path, error=VERIFICATION_FAILED, status_code=401
        )
    return session_or_change(next_path, outcome)


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
    """Confirm the enrolment, sign in (or go on to a required change), show the codes once."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    pending = challenge_of(request)
    if pending is None or pending[0] != "enrol":
        return _start_over(next_path)
    if "totp" not in challenge_methods(request):
        return _start_over(next_path)
    try:
        codes, outcome = await runtime.identity.confirm_enrolment(
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
    if outcome is None:  # under a challenge warden always signs in or asks for a change
        raise HTTPException(status_code=500)
    return _enrolled(request, list(codes), outcome, next_path)


def _enrolled(
    request: Request,
    codes: list[str],
    outcome: TokenPair | PasswordChangeIssued,
    next_path: str,
) -> Response:
    """After a forced enrolment: the codes once, with the session (or the change), or straight on."""
    if not codes:
        # Not the user's first second factor (one no longer allowed was replaced):
        # warden keeps their recovery codes and returns none, so none are shown.
        return session_or_change(next_path, outcome)
    change = outcome if isinstance(outcome, PasswordChangeIssued) else None
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "auth_recovery_codes.html",
        {
            "recovery_codes": codes,
            # With a change still to make, the codes lead to the change screen.
            "next_path": next_path if change is None else change_path(change, next_path),
            "password_change": change is not None,
            "error": None,
        },
    )
    # Shown once: never cached, so neither Back nor the history shows them again.
    private_headers(response)
    if isinstance(outcome, PasswordChangeIssued):
        _set_challenge(response, "change", outcome.token, outcome.expires_in)
    else:
        set_session_cookies(response, outcome)
        _clear_challenge(response)
    return response


@router.post(CHALLENGE_WEBAUTHN_OPTIONS_PATH)
async def challenge_webauthn_options(request: Request) -> JSONResponse:
    """The options for the challenge's key: one the user holds, or a new one to enrol."""
    runtime = identity_runtime(request)
    pending = challenge_of(request)
    if (
        pending is None
        or pending[0] not in ("verify", "enrol")
        or "webauthn" not in challenge_methods(request)
    ):
        return options_refused(VERIFICATION_FAILED)
    purpose, token = pending
    ip = source_ip(runtime, request)
    try:
        if purpose == "verify":
            options = await runtime.identity.begin_webauthn_sign_in(token, source_ip=ip)
        else:
            options = await runtime.identity.begin_webauthn_registration(
                challenge=token, source_ip=ip
            )
    except AuthenticationRefused:
        return options_refused(VERIFICATION_FAILED)
    return options_response(options)


@router.post(CHALLENGE_WEBAUTHN_PATH)
async def challenge_webauthn(request: Request) -> Response:
    """Finish the challenge with the browser's answer: sign in, or enrol the key and sign in."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    pending = challenge_of(request)
    if (
        pending is None
        or pending[0] not in ("verify", "enrol")
        or "webauthn" not in challenge_methods(request)
    ):
        return _start_over(next_path)
    purpose, token = pending
    ip = source_ip(runtime, request)
    try:
        if purpose == "verify":
            outcome = await runtime.identity.verify_webauthn(
                token, credential_of(form), source_ip=ip
            )
            return session_or_change(next_path, outcome)
        enrolled: WebAuthnEnrolled = await runtime.identity.finish_webauthn_registration(
            credential_of(form), nickname=nickname_of(form), challenge=token, source_ip=ip
        )
    except (AuthenticationRefused, WebAuthnError, WebAuthnUnavailable):
        return _challenge_page(
            request, purpose, next_path, error=VERIFICATION_FAILED, status_code=401
        )
    issued = enrolled.password_change or enrolled.tokens
    if issued is None:  # under a challenge warden always signs in or asks for a change
        raise HTTPException(status_code=500)
    return _enrolled(request, list(enrolled.recovery_codes), issued, next_path)


@router.post(CHALLENGE_CHANGE_PATH)
async def challenge_change(request: Request) -> Response:
    """Set the new password warden requires, then sign in."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    reason = str(form.get("reason") or "")
    pending = challenge_of(request)
    if pending is None or pending[0] != "change":
        return _start_over(next_path)
    password, confirm = form.get("password"), form.get("confirm")
    if not isinstance(password, str) or password != confirm:
        return _challenge_page(
            request, "change", next_path, error=PASSWORDS_DIFFER, status_code=400, reason=reason
        )
    try:
        pair = await runtime.identity.complete_password_change(
            pending[1], password, source_ip=source_ip(runtime, request)
        )
    except PasswordRejected as exc:  # the challenge stays, less one attempt
        return _challenge_page(
            request, "change", next_path, error=refusal_text(exc), status_code=400, reason=reason
        )
    except AuthenticationRefused:
        return _start_over(next_path)
    response = signed_in_response(next_path, pair)
    _clear_challenge(response)
    return response


# -- Passkey sign-in -------------------------------------------------------------------------


@router.post(PASSKEY_SIGN_IN_OPTIONS_PATH)
async def passkey_sign_in_options(request: Request) -> JSONResponse:
    """The options for signing in with a passkey alone (a button, or the browser's autofill)."""
    runtime = identity_runtime(request)
    try:
        options = await runtime.identity.begin_passkey_sign_in(
            source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        return options_refused(PASSKEY_SIGN_IN_FAILED)
    return options_response(options)


@router.post(PASSKEY_SIGN_IN_PATH)
async def passkey_sign_in(request: Request) -> Response:
    """Sign in with the browser's passkey answer; warden decides who may (ruling CS14)."""
    runtime = identity_runtime(request)
    form = await request.form()
    next_path = safe_next(form.get("next"))
    try:
        pair = await runtime.identity.passkey_sign_in(
            credential_of(form), source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        # One answer for every refusal; warden's audit trail records which it was.
        return await sign_in_page(
            request, error=PASSKEY_SIGN_IN_FAILED, status_code=401, next_path=next_path
        )
    response = signed_in_response(next_path, pair)
    forget_form_token(response, SIGN_IN_PATH)
    return response


# -- Registering a passkey from an administrator's link -------------------------------------


def passkey_registered_page(
    request: Request,
    template_context: dict[str, Any],
    *,
    error: str | None = None,
    status_code: int = 200,
    codes: list[str] | None = None,
    done: bool = False,
) -> HTMLResponse:
    """auth_passkey_register.html: register a passkey, or, once done, the codes and sign-in.

    Shared by a registration link and a ``passkey_only`` invitation
    (:mod:`spark.web.endpoints.lifecycle`); ``template_context`` names the
    form's ``kind``, ``action``, ``options_url`` and ``token``.
    """
    return form_page(
        request,
        "auth_passkey_register.html",
        {
            **template_context,
            "error": error,
            "done": done,
            "recovery_codes": codes or [],
        },
        status_code=status_code,
    )


def _registration_page(
    request: Request,
    token: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    codes: list[str] | None = None,
    done: bool = False,
) -> HTMLResponse:
    return passkey_registered_page(
        request,
        {
            "kind": "link",
            "action": PASSKEY_REGISTRATION_PATH,
            "options_url": PASSKEY_REGISTRATION_OPTIONS_PATH,
            "token": token,
        },
        error=error,
        status_code=status_code,
        codes=codes,
        done=done,
    )


@router.get(PASSKEY_REGISTRATION_PATH, response_class=HTMLResponse)
async def passkey_registration_page(request: Request) -> HTMLResponse:
    """The screen a registration link opens: register a passkey, without signing in."""
    identity_runtime(request)
    token = request.query_params.get("token", "")
    if not token:
        return _registration_page(request, "", error=LINK_NOT_VALID, status_code=400)
    return _registration_page(request, token)


@router.post(PASSKEY_REGISTRATION_OPTIONS_PATH)
async def passkey_registration_options(request: Request) -> JSONResponse:
    """The options for the link's passkey; the link stays usable."""
    runtime = identity_runtime(request)
    body = await json_body(request)
    try:
        options = await runtime.identity.begin_passkey_registration(
            body.get("token"), source_ip=source_ip(runtime, request)
        )
    except AuthenticationRefused:
        return options_refused(LINK_NOT_VALID)
    except WebAuthnError:  # the user holds as many keys as allowed
        return options_refused(KEY_LIMIT, 409)
    return options_response(options)


@router.post(PASSKEY_REGISTRATION_PATH)
async def passkey_registration(request: Request) -> Response:
    """Register the passkey and use the link up; the recovery codes are shown once."""
    runtime = identity_runtime(request)
    form = await request.form()
    raw = form.get("token")
    token = raw if isinstance(raw, str) else ""
    try:
        enrolled = await runtime.identity.finish_passkey_registration(
            token,
            credential_of(form),
            nickname=nickname_of(form),
            source_ip=source_ip(runtime, request),
        )
    except AuthenticationRefused:
        return _registration_page(request, "", error=LINK_NOT_VALID, status_code=401)
    except WebAuthnError:  # not a passkey, or not accepted: the link stays usable
        return _registration_page(request, token, error=KEY_NOT_REGISTERED, status_code=400)
    response = _registration_page(request, "", codes=list(enrolled.recovery_codes), done=True)
    forget_form_token(response, PASSKEY_REGISTRATION_PATH)
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
