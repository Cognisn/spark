"""User, role, invitation and API key administration (server mode, ``spark.users.admin``).

SPARK's screens over warden's Python API; warden audits every change itself
(with the administrator as actor), so nothing here records audit events.

* ``/admin/users``: users, oldest first, a page at a time (warden's cursor),
  optionally of one status; create a user with a password or for single
  sign-on, with roles. ``/admin/users/{id}``: one user, with their roles
  (assign and revoke), their status (suspend, reactivate, deactivate), a TOTP
  reset and a password reset link; their passkeys and security keys
  (``_admin_user_passkeys.html``), a passkey registration link
  (``POST /admin/users/{id}/passkey-link``, for a user who has lost their
  passkeys or signs in with one only) and a reset of every second factor
  (``POST /admin/users/{id}/mfa-reset``: every key, the authenticator app and
  the recovery codes, and their sessions end). warden 0.2.0 has no
  administrator's removal of a single key, so the reset is how an
  administrator removes them.
* ``/admin/invitations``: pending invitations; invite with staged roles and a
  sign-in method (a passkey only, where passkeys work here), resend (the old
  link stops working) and revoke.
* ``/admin/api-keys``: every user's API keys; revoke one.
* ``/admin/sso``: single sign-on through warden's settings: the issuer URL,
  client ID, client secret (to the secrets store, never shown again) and
  whether it is on, the redirect URI to register, and a Test of the saved
  settings against the provider that signs nobody in. Switching it off while
  any user signs in only through it asks first and names them. Until it is on
  and configured (:func:`spark.identity.sso.available`) the user screens offer
  no single sign-on method, and flag SSO-only users so their method can be
  changed (``POST /admin/users/{id}/sign-in-method``).

warden refuses to leave no active administrator (``LastAdministratorError``);
the screen says so and nothing changes. Without a mail service warden's mails
are not sent (:mod:`spark.identity.mail`), so an invitation or reset link is
shown to the administrator once instead, on a page that is never cached and
never logged; a deployment with its own mail sender gets a notice that it was
sent.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from warden.identity import (
    InvitationError,
    LastAdministratorError,
    MailError,
    PasskeyLinkRefused,
    RoleError,
    SignInMethod,
    StagedGrant,
    User,
    UserError,
    UserStatus,
    WebAuthnUnavailable,
)

from spark.identity import sso
from spark.identity.access import SparkIdentity, require_permission
from spark.identity.mail import captured, link_in
from spark.identity.permissions import USERS_ADMIN
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import INVITATION_PATH, PASSKEY_REGISTRATION_PATH, RESET_PATH, source_ip
from spark.web.endpoints.account import key_kind, screen
from spark.web.endpoints.identity import identity_runtime, passkeys_available, redirect

router = APIRouter()

USERS_PATH = "/admin/users"
INVITATIONS_PATH = "/admin/invitations"
KEYS_PATH = "/admin/api-keys"
PAGE_SIZE = 25

LAST_ADMINISTRATOR = (
    "Refused: this user is the last active administrator, and Spark always keeps one. "
    "Make another user an administrator first."
)
NOT_SENT = (
    "No reset link was sent: the user is not active, or signs in only through single sign-on."
)
DONE = {
    "status": "The user's status is changed.",
    "role-assigned": "The role is assigned.",
    "role-revoked": "The role is revoked.",
    "totp-reset": (
        "Two-step verification is reset and the user's sessions are ended. They set it up "
        "again at their next sign-in."
    ),
    "created": "The user is created.",
    "revoked": "Revoked.",
    "sent": "The link is sent.",
    "method": "The sign-in method is changed.",
    "saved": "The single sign-on settings are saved.",
    "mfa-reset": (
        "Every second factor is removed and the user's sessions are ended. They set one up "
        "again at their next sign-in, or with a passkey registration link."
    ),
}
NO_PASSKEY_LINK = (
    "No link was sent: the user is not active, or signs in only through single sign-on."
)
PASSKEYS_OFF = "Passkeys cannot be used on this server, so no link was sent."
SSO_PATH = "/admin/sso"
SSO_OFF = "Single sign-on is not switched on, so a user cannot be set to sign in only through it."
SSO_OFF_FLAG = "Cannot sign in: single sign-on is off"
SSO_ONLY_ASK = (
    "These users sign in only through single sign-on and cannot sign in once it is "
    "switched off. Change their sign-in method first, or switch it off anyway."
)
# The statuses an administrator sets, and those the list filters by.
_STATUSES = {s.value: s for s in (UserStatus.ACTIVE, UserStatus.SUSPENDED, UserStatus.DEACTIVATED)}
_FILTERS = {s.value: s for s in UserStatus}
# The sign-in methods an administrator sets on these screens.
_SETTABLE = {m.value: m for m in (SignInMethod.PASSWORD_AND_SSO, SignInMethod.SSO_ONLY)}

administrator = require_permission(USERS_ADMIN)


def _runtime(request: Request) -> IdentityRuntime:
    return identity_runtime(request)


def _actor(identity: SparkIdentity) -> str:
    return identity.warden_user_id


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404) from None


def _method(value: str) -> SignInMethod:
    try:
        return SignInMethod(value)
    except ValueError:
        return SignInMethod.PASSWORD_AND_SSO


def _role_names(runtime: IdentityRuntime) -> list[str]:
    return sorted(role.name for role in runtime.identity.permissions.roles())


def _mailed(runtime: IdentityRuntime, messages: list[Any], path: str) -> dict[str, Any]:
    """What to tell the administrator about a mailed link: the link once, or that it was sent."""
    link = link_in(messages, runtime.config.http.public_base_url, path)
    return {"mailed_link": link, "notice": None if link else DONE["sent"]}


# -- Users ----------------------------------------------------------------------------------


def _sso_only_refused(method: SignInMethod | None, sso_on: bool) -> bool:
    """SSO-only is not a method to set while single sign-on is unavailable."""
    return method is SignInMethod.SSO_ONLY and not sso_on


async def _users_page(
    request: Request,
    runtime: IdentityRuntime,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    params = request.query_params
    status = _FILTERS.get(params.get("status", ""))
    users_api = runtime.identity.users
    try:
        users = await users_api.list(
            status=status, limit=PAGE_SIZE + 1, after=params.get("after") or None
        )
    except UserError:
        raise HTTPException(status_code=404) from None
    more = len(users) > PAGE_SIZE
    users = users[:PAGE_SIZE]
    roles = {str(u.id): await runtime.identity.roles.roles_of(u.id) for u in users}
    return screen(
        request,
        "admin_users.html",
        {
            "users": users,
            "roles": roles,
            "status": status.value if status else "",
            "next_after": users_api.cursor(users[-1]) if more and users else None,
            "role_names": _role_names(runtime),
            "error": error,
            "sso_available": await sso.available(runtime.identity),
            "sso_off_flag": SSO_OFF_FLAG,
            "sso_path": SSO_PATH,
        },
        status_code=status_code,
    )


@router.get(USERS_PATH, response_class=HTMLResponse)
async def users_page(request: Request, _: SparkIdentity = Depends(administrator)) -> HTMLResponse:
    return await _users_page(request, _runtime(request))


@router.post(USERS_PATH)
async def create_user(
    request: Request, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Create an active user with a password (or for single sign-on only) and roles."""
    runtime = _runtime(request)
    form = await request.form()
    method = _method(_text(form.get("method")))
    password = _text(form.get("password")) or None
    roles = [r for r in form.getlist("roles") if isinstance(r, str)]
    if _sso_only_refused(method, await sso.available(runtime.identity)):
        return await _users_page(request, runtime, error=SSO_OFF, status_code=400)
    if method is SignInMethod.PASSWORD_AND_SSO and password is None:
        return await _users_page(
            request,
            runtime,
            error="A user who signs in with a password needs one.",
            status_code=400,
        )
    identity_api = runtime.identity
    try:
        async with identity_api.db.transaction() as tx:
            user = await identity_api.users.create(
                _text(form.get("email")),
                actor=_actor(identity),
                display_name=_text(form.get("display_name")) or None,
                sign_in_method=method,
                password=password if method is SignInMethod.PASSWORD_AND_SSO else None,
                conn=tx,
            )
            for role in roles:
                await identity_api.roles.assign(user.id, role, actor=_actor(identity), conn=tx)
    except (UserError, RoleError, KeyError) as exc:
        return await _users_page(request, runtime, error=_message(exc), status_code=400)
    return redirect(f"{USERS_PATH}/{user.id}?done=created")


def _message(exc: Exception) -> str:
    """warden's fixed message for a refusal (they never echo the request)."""
    if isinstance(exc, KeyError):
        return "That role is not defined."
    text = str(exc)
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


async def _user_or_404(runtime: IdentityRuntime, user_id: str) -> User:
    user = await runtime.identity.users.get(_uuid(user_id))
    if user is None:
        raise HTTPException(status_code=404)
    return user


async def _user_page(
    request: Request,
    runtime: IdentityRuntime,
    user: User,
    *,
    error: str | None = None,
    status_code: int = 200,
    extra: dict[str, Any] | None = None,
) -> HTMLResponse:
    context: dict[str, Any] = {
        "user": user,
        "assignments": await runtime.identity.roles.assignments_of(user.id),
        "role_names": _role_names(runtime),
        "mfa_enrolled": await runtime.identity.mfa.has_confirmed_totp(user.id),
        "notice": DONE.get(request.query_params.get("done", "")),
        "error": error,
        "mailed_link": None,
        "sso_available": await sso.available(runtime.identity),
        "sso_off_flag": SSO_OFF_FLAG,
        # _admin_user_passkeys.html
        "passkeys": await runtime.identity.webauthn_credentials.list(user.id),
        "key_kind": key_kind,
        "webauthn": await passkeys_available(runtime),
    }
    context.update(extra or {})
    return screen(
        request,
        "admin_user.html",
        context,
        status_code=status_code,
        private=context["mailed_link"] is not None,
    )


@router.get(USERS_PATH + "/{user_id}", response_class=HTMLResponse)
async def user_page(
    request: Request, user_id: str, _: SparkIdentity = Depends(administrator)
) -> HTMLResponse:
    runtime = _runtime(request)
    return await _user_page(request, runtime, await _user_or_404(runtime, user_id))


@router.post(USERS_PATH + "/{user_id}/status")
async def set_status(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Suspend, reactivate or deactivate a user (never the last active administrator)."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    form = await request.form()
    status = _STATUSES.get(_text(form.get("status")))
    if status is None:
        return await _user_page(
            request, runtime, user, error="That is not a status to set.", status_code=400
        )
    try:
        await runtime.identity.users.set_status(user.id, status, actor=_actor(identity))
    except LastAdministratorError:
        return await _user_page(request, runtime, user, error=LAST_ADMINISTRATOR, status_code=409)
    return redirect(f"{USERS_PATH}/{user.id}?done=status")


@router.post(USERS_PATH + "/{user_id}/sign-in-method")
async def set_sign_in_method(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Change how a user signs in (SSO-only only while single sign-on is available)."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    form = await request.form()
    method = _SETTABLE.get(_text(form.get("method")))
    if method is None:
        return await _user_page(
            request, runtime, user, error="That is not a sign-in method to set.", status_code=400
        )
    if _sso_only_refused(method, await sso.available(runtime.identity)):
        return await _user_page(request, runtime, user, error=SSO_OFF, status_code=400)
    try:
        await runtime.identity.users.set_sign_in_method(
            user.id, method, actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except UserError as exc:
        return await _user_page(request, runtime, user, error=_message(exc), status_code=409)
    return redirect(f"{USERS_PATH}/{user.id}?done=method")


@router.post(USERS_PATH + "/{user_id}/roles")
async def assign_role(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    form = await request.form()
    try:
        await runtime.identity.roles.assign(
            user.id, _text(form.get("role")), actor=_actor(identity)
        )
    except (RoleError, KeyError) as exc:
        return await _user_page(request, runtime, user, error=_message(exc), status_code=400)
    return redirect(f"{USERS_PATH}/{user.id}?done=role-assigned")


@router.post(USERS_PATH + "/{user_id}/roles/{role}/revoke")
async def revoke_role(
    request: Request, user_id: str, role: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Revoke a role (never the last active administrator's administrator role)."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    try:
        await runtime.identity.roles.revoke(user.id, role, actor=_actor(identity))
    except LastAdministratorError:
        return await _user_page(request, runtime, user, error=LAST_ADMINISTRATOR, status_code=409)
    return redirect(f"{USERS_PATH}/{user.id}?done=role-revoked")


@router.post(USERS_PATH + "/{user_id}/totp-reset")
async def reset_totp(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Remove the user's authenticator and recovery codes and end their sessions."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    await runtime.identity.reset_totp(
        user.id, actor=_actor(identity), source_ip=source_ip(runtime, request)
    )
    return redirect(f"{USERS_PATH}/{user.id}?done=totp-reset")


@router.post(USERS_PATH + "/{user_id}/password-reset")
async def send_password_reset(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Send the user a reset link (or, without a mail service, show it once)."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    try:
        with captured() as messages:
            reason = await runtime.identity.resets.request(
                user.email, actor=_actor(identity), source_ip=source_ip(runtime, request)
            )
    except MailError:
        return await _user_page(
            request, runtime, user, error="The link could not be sent.", status_code=502
        )
    if reason is not None:
        return await _user_page(request, runtime, user, error=NOT_SENT, status_code=409)
    return await _user_page(request, runtime, user, extra=_mailed(runtime, messages, RESET_PATH))


@router.post(USERS_PATH + "/{user_id}/passkey-link")
async def send_passkey_link(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Send the user a passkey registration link (or, without a mail service, show it once)."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    try:
        with captured() as messages:
            await runtime.identity.send_passkey_registration(
                user.id, actor=_actor(identity), source_ip=source_ip(runtime, request)
            )
    except PasskeyLinkRefused:
        return await _user_page(request, runtime, user, error=NO_PASSKEY_LINK, status_code=409)
    except WebAuthnUnavailable:
        return await _user_page(request, runtime, user, error=PASSKEYS_OFF, status_code=409)
    except MailError:
        return await _user_page(
            request, runtime, user, error="The link could not be sent.", status_code=502
        )
    return await _user_page(
        request, runtime, user, extra=_mailed(runtime, messages, PASSKEY_REGISTRATION_PATH)
    )


@router.post(USERS_PATH + "/{user_id}/mfa-reset")
async def reset_mfa(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Remove every second factor of the user (keys, app, codes) and end their sessions."""
    runtime = _runtime(request)
    user = await _user_or_404(runtime, user_id)
    await runtime.identity.reset_mfa(
        user.id, actor=_actor(identity), source_ip=source_ip(runtime, request)
    )
    return redirect(f"{USERS_PATH}/{user.id}?done=mfa-reset")


# -- Invitations ----------------------------------------------------------------------------


async def _invitations_page(
    request: Request,
    runtime: IdentityRuntime,
    *,
    error: str | None = None,
    status_code: int = 200,
    extra: dict[str, Any] | None = None,
) -> HTMLResponse:
    context: dict[str, Any] = {
        "pending": await runtime.identity.invitations.pending(),
        "role_names": _role_names(runtime),
        "notice": DONE.get(request.query_params.get("done", "")),
        "error": error,
        "mailed_link": None,
        "sso_available": await sso.available(runtime.identity),
        "webauthn": await passkeys_available(runtime),
    }
    context.update(extra or {})
    return screen(
        request,
        "admin_invitations.html",
        context,
        status_code=status_code,
        private=context["mailed_link"] is not None,
    )


@router.get(INVITATIONS_PATH, response_class=HTMLResponse)
async def invitations_page(
    request: Request, _: SparkIdentity = Depends(administrator)
) -> HTMLResponse:
    return await _invitations_page(request, _runtime(request))


@router.post(INVITATIONS_PATH)
async def invite(request: Request, identity: SparkIdentity = Depends(administrator)) -> Response:
    """Invite an address with staged roles; the link is mailed, or shown here once."""
    runtime = _runtime(request)
    form = await request.form()
    grants = [StagedGrant(r) for r in form.getlist("roles") if isinstance(r, str)]
    method = _method(_text(form.get("method")))
    if _sso_only_refused(method, await sso.available(runtime.identity)):
        return await _invitations_page(request, runtime, error=SSO_OFF, status_code=400)
    try:
        with captured() as messages:
            await runtime.identity.invitations.create(
                _text(form.get("email")),
                actor=_actor(identity),
                display_name=_text(form.get("display_name")) or None,
                sign_in_method=method,
                grants=grants,
                source_ip=source_ip(runtime, request),
            )
    except (InvitationError, RoleError, UserError) as exc:
        return await _invitations_page(request, runtime, error=_message(exc), status_code=400)
    except MailError:
        return await _invitations_page(
            request, runtime, error="The invitation could not be sent.", status_code=502
        )
    return await _invitations_page(
        request, runtime, extra=_mailed(runtime, messages, INVITATION_PATH)
    )


@router.post(INVITATIONS_PATH + "/{user_id}/resend")
async def resend_invitation(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """A new link; the old one stops working."""
    runtime = _runtime(request)
    try:
        with captured() as messages:
            await runtime.identity.invitations.resend(
                _uuid(user_id), actor=_actor(identity), source_ip=source_ip(runtime, request)
            )
    except InvitationError as exc:
        return await _invitations_page(request, runtime, error=_message(exc), status_code=409)
    except MailError:
        return await _invitations_page(
            request, runtime, error="The invitation could not be sent.", status_code=502
        )
    return await _invitations_page(
        request, runtime, extra=_mailed(runtime, messages, INVITATION_PATH)
    )


@router.post(INVITATIONS_PATH + "/{user_id}/revoke")
async def revoke_invitation(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    try:
        await runtime.identity.invitations.revoke(
            _uuid(user_id), actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except InvitationError as exc:
        return await _invitations_page(request, runtime, error=_message(exc), status_code=409)
    return redirect(f"{INVITATIONS_PATH}?done=revoked")


# -- API keys -------------------------------------------------------------------------------


@router.get(KEYS_PATH, response_class=HTMLResponse)
async def keys_page(request: Request, _: SparkIdentity = Depends(administrator)) -> HTMLResponse:
    runtime = _runtime(request)
    keys = await runtime.identity.api_keys.list()
    owners: dict[str, str] = {}
    for user_id in {k.user_id for k in keys}:
        user = await runtime.identity.users.get(user_id)
        owners[str(user_id)] = user.email if user is not None else str(user_id)
    return screen(
        request,
        "admin_api_keys.html",
        {
            "keys": keys,
            "owners": owners,
            "revoke_base": KEYS_PATH,
            "notice": DONE.get(request.query_params.get("done", "")),
        },
    )


@router.post(KEYS_PATH + "/{key_id}/revoke")
async def revoke_key(
    request: Request, key_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    keys = runtime.identity.api_keys
    key = await keys.get(_uuid(key_id))
    if key is None:
        raise HTTPException(status_code=404)
    await keys.revoke(key.id, actor=_actor(identity), source_ip=source_ip(runtime, request))
    return redirect(f"{KEYS_PATH}?done=revoked")


# -- Single sign-on -------------------------------------------------------------------------


def _checks(report: Any) -> list[dict[str, Any]]:
    """warden's configuration report, worded from its fixed names and reasons only."""
    rows = []
    for check in report.checks:
        label, result = sso.check_text(check.name, check.ok, check.reason)
        rows.append({"label": label, "ok": check.ok, "result": result})
    return rows


async def _sso_page(
    request: Request,
    runtime: IdentityRuntime,
    *,
    error: str | None = None,
    status_code: int = 200,
    extra: dict[str, Any] | None = None,
) -> HTMLResponse:
    current = await sso.state(runtime.identity)
    settings = current.settings
    context: dict[str, Any] = {
        "issuer_url": settings.issuer_url or "",
        "client_id": settings.client_id or "",
        "enabled": settings.enabled,
        "secret_set": current.secret_set,
        "available": current.available,
        "callback_url": runtime.config.http.sso_callback_url,
        "notice": DONE["saved"] if request.query_params.get("done") == "saved" else None,
        "error": error,
        "checks": None,
        "checks_ok": False,
        "confirm_users": None,
        "secret_dropped": False,
        "guard": None,
    }
    context.update(extra or {})
    # The page never holds the secret; it is kept out of caches all the same.
    return screen(request, "admin_sso.html", context, status_code=status_code, private=True)


@router.get(SSO_PATH, response_class=HTMLResponse)
async def sso_page(request: Request, _: SparkIdentity = Depends(administrator)) -> HTMLResponse:
    return await _sso_page(request, _runtime(request))


@router.post(SSO_PATH)
async def save_sso(request: Request, identity: SparkIdentity = Depends(administrator)) -> Response:
    """Save the settings; a new client secret goes to the secrets store, a blank keeps it.

    Switching single sign-on off while a suspended or invited user signs in only
    through it needs ``confirm=yes``; without it nothing is saved and the page
    names them. While an active user (or the instance default) does, warden
    refuses whatever is confirmed (its guard's 409): the page says so in plain
    words and lists those users, each linked to their page, to move first.
    """
    runtime = _runtime(request)
    form = await request.form()
    secret = form.get("client_secret")
    change = sso.SsoChange(
        issuer_url=_text(form.get("issuer_url")) or None,
        client_id=_text(form.get("client_id")) or None,
        enabled=form.get("enabled") == "on",
        client_secret=secret if isinstance(secret, str) and secret != "" else None,
    )
    typed = {"issuer_url": change.issuer_url or "", "client_id": change.client_id or ""}
    current = await sso.state(runtime.identity)
    if current.settings.enabled and not change.enabled and form.get("confirm") != "yes":
        users = await sso.sso_only_users(runtime.identity)
        # Active users are warden's to refuse (below); confirming could not override it.
        if users and not any(u.status is UserStatus.ACTIVE for u in users):
            return await _sso_page(
                request,
                runtime,
                error=SSO_ONLY_ASK,
                status_code=409,
                extra={
                    **typed,
                    "confirm_users": users,
                    "secret_dropped": change.client_secret is not None,
                },
            )
    try:
        await sso.save(
            runtime.identity, change, actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except sso.SsoStillRequired as exc:
        return await _sso_page(
            request, runtime, error=str(exc), status_code=409, extra={**typed, "guard": exc}
        )
    except ValueError as exc:
        return await _sso_page(request, runtime, error=str(exc), status_code=400, extra=typed)
    return redirect(f"{SSO_PATH}?done=saved")


@router.post(SSO_PATH + "/test", response_class=HTMLResponse)
async def test_sso(request: Request, identity: SparkIdentity = Depends(administrator)) -> Response:
    """Check the saved settings against the provider (warden's test); signs nobody in."""
    runtime = _runtime(request)
    report = await runtime.identity.sso.test_configuration(
        actor=_actor(identity), source_ip=source_ip(runtime, request)
    )
    return await _sso_page(
        request, runtime, extra={"checks": _checks(report), "checks_ok": report.ok}
    )
