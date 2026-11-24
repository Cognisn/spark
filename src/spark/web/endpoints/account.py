"""The signed-in user's own screens (server mode): profile, password, second factors, API keys.

* ``GET /account``: the user's email, name, roles and how they sign in.
* ``POST /account/password``: change the password through warden, which ends
  the user's other sessions and keeps this one.
* ``GET /account/mfa``: account security, the user's second factors: the
  authenticator app, and their passkeys and security keys.
* ``POST /account/mfa`` and ``POST /account/mfa/confirm``: set up an
  authenticator app while signed in, for a user with none confirmed; the
  recovery codes are shown once. warden 0.1.0 cannot replace a confirmed
  enrolment for its own user, so replacing one is an administrator's TOTP
  reset (the user enrols again at the next sign-in).
* ``POST /account/passkeys/options`` and ``POST /account/passkeys``: register a
  passkey or security key with a name (``webauthn.js`` fetches the options,
  then the form posts the browser's answer); the recovery codes come with a
  user's first second factor, shown once.
  ``POST /account/passkeys/{id}/rename`` and ``/remove``: the user's own keys
  only (another's is not found). warden refuses removing the last second
  factor, since every password sign-in needs one (ruling CS14).
* ``GET/POST /account/api-keys`` and ``POST /account/api-keys/{id}/revoke``:
  the user's own API keys, behind ``spark.api_keys`` (warden has no
  self-service route, question 19). A key's scopes may not exceed the
  permissions of the request creating it, so a key can never mint a wider
  key; the key is shown once, and only the user's own keys can be revoked
  here.

Every route needs a signed-in user (the auth middleware sends a page load
without one to sign-in), answers 404 in personal mode, and leaves the audit
to warden, which records each of these changes itself. Changing the password
and the authenticator need a browser session, not an API key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from warden.identity import (
    ApiKeyError,
    LastSecondFactorError,
    MfaError,
    PasswordRejected,
    Principal,
    SignInMethod,
    WebAuthnCredential,
    WebAuthnError,
    WebAuthnRefusal,
    WebAuthnUnavailable,
)

from spark.identity.access import SparkIdentity, current_identity, require_permission
from spark.identity.passwords import refusal_text
from spark.identity.permissions import API_KEYS
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import principal_of, source_ip, via_api_key
from spark.web.endpoints.identity import (
    KEY_LIMIT,
    KEY_NOT_REGISTERED,
    credential_of,
    identity_runtime,
    nickname_of,
    options_refused,
    options_response,
    passkeys_available,
    private_headers,
    redirect,
    shown_back,
    sso_enabled,
)
from spark.web.qr import qr_data_uri

router = APIRouter()

ACCOUNT_PATH = "/account"
KEYS_PATH = "/account/api-keys"
SECURITY_PATH = ACCOUNT_PATH + "/mfa"
PASSKEYS_PATH = ACCOUNT_PATH + "/passkeys"

# Fixed notices after a change (?done=...); nothing from the URL is echoed.
DONE = {
    "password": "Your password is changed, and your other sessions are signed out.",
    "mfa-exists": (
        "An authenticator app is already set up. To replace it, ask an administrator "
        "to reset it."
    ),
    "revoked": "The key is revoked.",
    "passkey-added": "The passkey or security key is registered.",
    "passkey-renamed": "The name is changed.",
    "passkey-removed": "The passkey or security key is removed.",
}
LAST_FACTOR = (
    "This is your last second factor, and every password sign-in needs one. "
    "Add another passkey, security key or authenticator app before removing it."
)
NAME_REFUSED = "A name has 1 to 64 characters, with no control characters."
# A refused registration, by warden's reason; any other is KEY_NOT_REGISTERED.
REGISTRATION_REFUSED = {
    WebAuthnRefusal.LIMIT_REACHED: KEY_LIMIT,
    WebAuthnRefusal.FACTOR_NOT_ALLOWED: (
        "Passkeys and security keys are not allowed as a second factor here."
    ),
    WebAuthnRefusal.SSO_ONLY: "This account signs in through single sign-on only.",
    WebAuthnRefusal.CREDENTIAL_EXISTS: "That passkey or security key is already registered.",
    WebAuthnRefusal.NICKNAME: NAME_REFUSED,
}
PASSKEYS_UNAVAILABLE = "Passkeys and security keys cannot be used on this server."
PASSWORD_REFUSED = {
    "wrong_password": "The current password is not right.",
    "not_permitted": "This account cannot change its password here.",
}
PASSWORDS_DIFFER = "The two new passwords do not match."
CODE_NOT_ACCEPTED = "That code was not accepted. Check the code and try again."
SCOPES_EXCEED = "A key's scopes may not exceed your own permissions."
SESSION_NEEDED = "This needs a browser session, not an API key."


def screen(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    private: bool = False,
) -> HTMLResponse:
    """A page in the app's own layout; ``private`` for one showing a secret once."""
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, template, {"notice": None, "error": None, **context}, status_code=status_code
    )
    if private:
        private_headers(response)
    return response


def signed_in(request: Request) -> tuple[IdentityRuntime, SparkIdentity]:
    """warden's runtime and the signed-in user (404 in personal mode, 401 without one)."""
    runtime = identity_runtime(request)
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return runtime, identity


def _session_principal(request: Request) -> Principal:
    """warden's principal of a browser session; 403 for an API key."""
    principal = principal_of(request)
    if principal is None or via_api_key(request):
        raise HTTPException(status_code=403, detail=SESSION_NEEDED)
    return principal


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# -- Profile and password -------------------------------------------------------------------


async def _account_page(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    runtime, identity = signed_in(request)
    user_id = uuid.UUID(identity.warden_user_id)
    user = await runtime.identity.users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return screen(
        request,
        "account.html",
        {
            "email": user.email,
            "display_name": user.display_name,
            "roles": sorted(identity.roles),
            "sso_only": user.sign_in_method is SignInMethod.SSO_ONLY,
            "sso_enabled": await sso_enabled(runtime),
            "mfa_enrolled": await runtime.identity.mfa.has_confirmed_totp(user_id),
            "may_use_keys": API_KEYS in identity.permissions,
            "min_length": runtime.config.identity.password_min_length,
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
    )


@router.get(ACCOUNT_PATH, response_class=HTMLResponse)
async def account_page(request: Request) -> HTMLResponse:
    return await _account_page(request)


@router.post(ACCOUNT_PATH + "/password")
async def change_password(request: Request) -> Response:
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    form = await request.form()
    password, confirm = _text(form.get("password")), _text(form.get("confirm"))
    if password != confirm:
        return await _account_page(request, error=PASSWORDS_DIFFER, status_code=400)
    try:
        reason = await runtime.identity.change_password(
            principal, form.get("current"), password, source_ip=source_ip(runtime, request)
        )
    except PasswordRejected as exc:  # the policy refused it; nothing changed
        return await _account_page(request, error=refusal_text(exc), status_code=400)
    if reason is not None:
        error = PASSWORD_REFUSED.get(reason, PASSWORD_REFUSED["not_permitted"])
        return await _account_page(request, error=error, status_code=400)
    return redirect(f"{ACCOUNT_PATH}?done=password")


# -- The user's own authenticator -----------------------------------------------------------


def key_kind(key: WebAuthnCredential) -> str:
    """How a key is described: a passkey signs in on its own; a security key does not."""
    return "Passkey" if key.passkey else "Security key"


async def _mfa_page(
    request: Request,
    *,
    secret: str = "",
    uri: str = "",
    error: str | None = None,
    codes: list[str] | None = None,
    codes_for: str = "totp",
    status_code: int = 200,
) -> HTMLResponse:
    """Account security: the overview, or a TOTP enrolment, or new recovery codes once."""
    runtime, identity = signed_in(request)
    user_id = uuid.UUID(identity.warden_user_id)
    return screen(
        request,
        "account_mfa.html",
        {
            "secret": secret,
            "uri": uri,
            "qr": qr_data_uri(uri) if uri else None,
            "recovery_codes": codes,
            "codes_for": codes_for,
            "mfa_enrolled": await runtime.identity.mfa.has_confirmed_totp(user_id),
            "passkeys": await runtime.identity.webauthn_credentials.list(user_id),
            "key_kind": key_kind,
            "webauthn": await passkeys_available(runtime),
            "passkeys_path": PASSKEYS_PATH,
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
        private=True,
    )


@router.get(SECURITY_PATH, response_class=HTMLResponse)
async def security_page(request: Request) -> HTMLResponse:
    """Account security: the user's authenticator app and passkeys."""
    return await _mfa_page(request)


@router.post(ACCOUNT_PATH + "/mfa")
async def begin_mfa(request: Request) -> Response:
    """Start an enrolment for a signed-in user who has none confirmed."""
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    try:
        enrolment = await runtime.identity.begin_enrolment(
            principal=principal, source_ip=source_ip(runtime, request)
        )
    except MfaError:
        return redirect(f"{ACCOUNT_PATH}?done=mfa-exists")
    return await _mfa_page(request, secret=enrolment.secret_base32, uri=enrolment.uri)


@router.post(ACCOUNT_PATH + "/mfa/confirm")
async def confirm_mfa(request: Request) -> Response:
    """Confirm the enrolment and show the recovery codes once."""
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    form = await request.form()
    try:
        codes, _ = await runtime.identity.confirm_enrolment(
            form.get("code"), principal=principal, source_ip=source_ip(runtime, request)
        )
    except MfaError:
        secret, uri = shown_back(form)
        return await _mfa_page(
            request, secret=secret, uri=uri, error=CODE_NOT_ACCEPTED, status_code=400
        )
    return await _mfa_page(request, codes=codes)


# -- The user's own passkeys and security keys ---------------------------------------------


def _passkey_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404) from None


def _refused(exc: WebAuthnError | WebAuthnUnavailable) -> tuple[str, int]:
    """A refused registration's fixed message and status."""
    if isinstance(exc, WebAuthnUnavailable):
        return PASSKEYS_UNAVAILABLE, 409
    text = REGISTRATION_REFUSED.get(exc.reason)
    return (text, 409) if text is not None else (KEY_NOT_REGISTERED, 400)


@router.post(PASSKEYS_PATH + "/options")
async def passkey_options(request: Request) -> JSONResponse:
    """The options for registering a passkey or security key (existing ones excluded)."""
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    try:
        options = await runtime.identity.begin_webauthn_registration(
            principal=principal, source_ip=source_ip(runtime, request)
        )
    except (WebAuthnError, WebAuthnUnavailable) as exc:
        return options_refused(*_refused(exc))
    return options_response(options)


@router.post(PASSKEYS_PATH)
async def register_passkey(request: Request) -> Response:
    """Register the browser's key with its name; a first second factor brings recovery codes."""
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    form = await request.form()
    try:
        enrolled = await runtime.identity.finish_webauthn_registration(
            credential_of(form),
            nickname=nickname_of(form),
            principal=principal,
            source_ip=source_ip(runtime, request),
        )
    except (WebAuthnError, WebAuthnUnavailable) as exc:
        text, status = _refused(exc)
        return await _mfa_page(request, error=text, status_code=status)
    if enrolled.recovery_codes:
        return await _mfa_page(request, codes=list(enrolled.recovery_codes), codes_for="passkey")
    return redirect(f"{SECURITY_PATH}?done=passkey-added")


@router.post(PASSKEYS_PATH + "/{key_id}/rename")
async def rename_passkey(request: Request, key_id: str) -> Response:
    """Name one of the user's own keys (a blank name clears it)."""
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    form = await request.form()
    try:
        renamed = await runtime.identity.rename_webauthn(
            principal, _passkey_id(key_id), nickname_of(form), source_ip=source_ip(runtime, request)
        )
    except WebAuthnError:
        return await _mfa_page(request, error=NAME_REFUSED, status_code=400)
    if renamed is None:
        raise HTTPException(status_code=404)
    return redirect(f"{SECURITY_PATH}?done=passkey-renamed")


@router.post(PASSKEYS_PATH + "/{key_id}/remove")
async def remove_passkey(request: Request, key_id: str) -> Response:
    """Remove one of the user's own keys, never their last second factor."""
    runtime, _ = signed_in(request)
    principal = _session_principal(request)
    try:
        removed = await runtime.identity.remove_webauthn(
            principal, _passkey_id(key_id), source_ip=source_ip(runtime, request)
        )
    except LastSecondFactorError:
        return await _mfa_page(request, error=LAST_FACTOR, status_code=409)
    if not removed:
        raise HTTPException(status_code=404)
    return redirect(f"{SECURITY_PATH}?done=passkey-removed")


# -- Own API keys ---------------------------------------------------------------------------


async def _keys_page(
    request: Request,
    identity: SparkIdentity,
    *,
    new_key: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    runtime = identity_runtime(request)
    keys = await runtime.identity.api_keys.list(user_id=uuid.UUID(identity.warden_user_id))
    registered = runtime.identity.permissions.permissions()
    scopes = [(name, registered[name]) for name in sorted(identity.permissions & registered.keys())]
    return screen(
        request,
        "account_api_keys.html",
        {
            "keys": keys,
            "revoke_base": KEYS_PATH,
            "scopes": scopes,
            "new_key": new_key,
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
        private=new_key is not None,
    )


@router.get(KEYS_PATH, response_class=HTMLResponse)
async def keys_page(
    request: Request, identity: SparkIdentity = Depends(require_permission(API_KEYS))
) -> HTMLResponse:
    return await _keys_page(request, identity)


def _expiry(value: str) -> datetime | None:
    days = int(value) if value.isdigit() and value.isascii() else 0
    return datetime.now(UTC) + timedelta(days=days) if 0 < days <= 3650 else None


@router.post(KEYS_PATH)
async def create_key(
    request: Request, identity: SparkIdentity = Depends(require_permission(API_KEYS))
) -> HTMLResponse:
    """Create a key for the user, scoped within the permissions this request holds."""
    runtime = identity_runtime(request)
    form = await request.form()
    scopes = frozenset(s for s in form.getlist("scopes") if isinstance(s, str))
    # The request's own permissions: for a session the user's, for an API key its scopes.
    if not scopes <= identity.permissions:
        return await _keys_page(request, identity, error=SCOPES_EXCEED, status_code=400)
    try:
        created = await runtime.identity.api_keys.create(
            uuid.UUID(identity.warden_user_id),
            _text(form.get("name")).strip(),
            scopes,
            actor=identity.warden_user_id,
            expires_at=_expiry(_text(form.get("expires_days"))),
            source_ip=source_ip(runtime, request),
        )
    except ApiKeyError as exc:
        return await _keys_page(request, identity, error=str(exc), status_code=400)
    return await _keys_page(request, identity, new_key=created.key)


def _key_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404) from None


@router.post(KEYS_PATH + "/{key_id}/revoke")
async def revoke_key(
    request: Request,
    key_id: str,
    identity: SparkIdentity = Depends(require_permission(API_KEYS)),
) -> Response:
    """Revoke one of the user's own keys; another user's key is not found."""
    runtime = identity_runtime(request)
    keys = runtime.identity.api_keys
    key = await keys.get(_key_id(key_id))
    if key is None or str(key.user_id) != identity.warden_user_id:
        raise HTTPException(status_code=404)
    await keys.revoke(key.id, actor=identity.warden_user_id, source_ip=source_ip(runtime, request))
    return redirect(f"{KEYS_PATH}?done=revoked")
