"""The signed-in user's own screens (server mode): profile, password, TOTP and API keys.

* ``GET /account``: the user's email, name, roles and how they sign in.
* ``POST /account/password``: change the password through warden, which ends
  the user's other sessions and keeps this one.
* ``POST /account/mfa`` and ``POST /account/mfa/confirm``: set up an
  authenticator app while signed in, for a user with none confirmed; the
  recovery codes are shown once. warden 0.1.0 cannot replace a confirmed
  enrolment for its own user, so replacing one is an administrator's TOTP
  reset (the user enrols again at the next sign-in).
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
from fastapi.responses import HTMLResponse, Response
from warden.identity import ApiKeyError, MfaError, Principal, SignInMethod

from spark.identity.access import SparkIdentity, current_identity, require_permission
from spark.identity.permissions import API_KEYS
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import principal_of, source_ip, via_api_key
from spark.web.endpoints.identity import identity_runtime, private_headers, redirect, shown_back
from spark.web.qr import qr_data_uri

router = APIRouter()

ACCOUNT_PATH = "/account"
KEYS_PATH = "/account/api-keys"

# Fixed notices after a change (?done=...); nothing from the URL is echoed.
DONE = {
    "password": "Your password is changed, and your other sessions are signed out.",
    "mfa-exists": (
        "An authenticator app is already set up. To replace it, ask an administrator "
        "to reset it."
    ),
    "revoked": "The key is revoked.",
}
PASSWORD_REFUSED = {
    "wrong_password": "The current password is not right.",
    "weak_password": "The new password is too short.",
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
    reason = await runtime.identity.change_password(
        principal, form.get("current"), password, source_ip=source_ip(runtime, request)
    )
    if reason is not None:
        error = PASSWORD_REFUSED.get(reason, PASSWORD_REFUSED["not_permitted"])
        return await _account_page(request, error=error, status_code=400)
    return redirect(f"{ACCOUNT_PATH}?done=password")


# -- The user's own authenticator -----------------------------------------------------------


def _mfa_page(
    request: Request,
    *,
    secret: str = "",
    uri: str = "",
    error: str | None = None,
    codes: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return screen(
        request,
        "account_mfa.html",
        {
            "secret": secret,
            "uri": uri,
            "qr": qr_data_uri(uri) if uri else None,
            "recovery_codes": codes,
            "error": error,
        },
        status_code=status_code,
        private=True,
    )


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
    return _mfa_page(request, secret=enrolment.secret_base32, uri=enrolment.uri)


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
        return _mfa_page(request, secret=secret, uri=uri, error=CODE_NOT_ACCEPTED, status_code=400)
    return _mfa_page(request, codes=codes)


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
