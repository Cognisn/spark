"""Live password feedback (server mode, warden 0.2.0, ruling CS23).

Every page that sets a password shows a checklist under the new-password field that
ticks as the user types. The rules come from warden for that place: the policy
description (``Identity.password_policy``) and, for what the browser cannot know
(a watch list, the breach range service, the user's own details), a check of the
candidate (``Identity.check_password``). The browser evaluates the description with
warden's packaged ``passwordrules.js``, served from here out of the installed package
and never copied into the repository. The server stays the authority: the page's own
submit is refused, with its reasons shown, whatever the checklist said.

Each place is bound to its own credential, as warden requires:

* an invitation page, with the invitation token the page holds;
* a reset page, with the reset token the page holds;
* the forced-change page, with the pending challenge in this browser's cookie;
* the account page, with the signed-in session.

Every answer is ``no-store``. A bad credential, a spent budget or an unknown place
all answer the same refusal, which reveals nothing. Nothing here stores, logs or
audits a candidate password.
"""

from __future__ import annotations

from typing import Any

import warden.static
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from warden.identity import AuthenticationRefused

from spark.identity.web import (
    PASSWORD_CHECK_SUFFIX,
    PASSWORD_POLICY_SUFFIX,
    PASSWORD_RULES_PATH,
    principal_of,
    source_ip,
    via_api_key,
)
from spark.web.endpoints.identity import challenge_of, identity_runtime, json_body

router = APIRouter()

INVITATION_BASE = "/auth/invitation"
RESET_BASE = "/auth/reset"
CHALLENGE_BASE = "/auth/challenge"
ACCOUNT_BASE = "/account"
PLACES = {
    "invitation": INVITATION_BASE,
    "reset": RESET_BASE,
    "challenge": CHALLENGE_BASE,
    "account": ACCOUNT_BASE,
}
NOT_AVAILABLE = "Password feedback is not available here."
NO_CANDIDATE = "Send the candidate password as text."
_NO_STORE = {"Cache-Control": "no-store"}
_SCRIPT_NAME = "passwordrules.js"


def _refused() -> JSONResponse:
    return JSONResponse({"detail": NOT_AVAILABLE}, status_code=403, headers=_NO_STORE)


@router.get(PASSWORD_RULES_PATH, include_in_schema=False)
async def password_rules_script() -> FileResponse:
    """warden's browser evaluator, read from the installed package at each request."""
    return FileResponse(
        warden.static.path(_SCRIPT_NAME),
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _answer(request: Request, credential: dict[str, Any], *, check: bool) -> Response:
    runtime = identity_runtime(request)
    body = await json_body(request)
    candidate = body.get("password")
    if check and not isinstance(candidate, str):
        return JSONResponse({"detail": NO_CANDIDATE}, status_code=400, headers=_NO_STORE)
    try:
        if check:
            feedback = await runtime.identity.check_password(
                candidate, source_ip=source_ip(runtime, request), **credential
            )
            return JSONResponse(feedback.body(), headers=_NO_STORE)
        advice = await runtime.identity.password_policy(
            source_ip=source_ip(runtime, request), **credential
        )
        return JSONResponse(advice.describe(), headers=_NO_STORE)
    except AuthenticationRefused:
        return _refused()


async def _token_of(request: Request) -> str | None:
    body = await json_body(request)
    token = body.get("token")
    return token if isinstance(token, str) and token else None


def _tokened(route: str, base: str, credential: str, *, check: bool) -> None:
    async def handle(request: Request) -> Response:
        token = await _token_of(request)
        if token is None:
            return _refused()
        return await _answer(request, {credential: token}, check=check)

    router.add_api_route(base + route, handle, methods=["POST"])


_tokened(PASSWORD_POLICY_SUFFIX, INVITATION_BASE, "invitation_token", check=False)
_tokened(PASSWORD_CHECK_SUFFIX, INVITATION_BASE, "invitation_token", check=True)
_tokened(PASSWORD_POLICY_SUFFIX, RESET_BASE, "reset_token", check=False)
_tokened(PASSWORD_CHECK_SUFFIX, RESET_BASE, "reset_token", check=True)


def _forced_change(request: Request) -> dict[str, Any] | None:
    found = challenge_of(request)
    if found is None or found[0] != "change":
        return None
    return {"change_challenge": found[1]}


def _signed_in(request: Request) -> dict[str, Any] | None:
    principal = principal_of(request)
    if principal is None or via_api_key(request):
        return None
    return {"principal": principal}


def _bound(place: str, route: str, find: Any, *, check: bool) -> None:
    async def handle(request: Request) -> Response:
        credential = find(request)
        if credential is None:
            return _refused()
        return await _answer(request, credential, check=check)

    router.add_api_route(PLACES[place] + route, handle, methods=["POST"])


_bound("challenge", PASSWORD_POLICY_SUFFIX, _forced_change, check=False)
_bound("challenge", PASSWORD_CHECK_SUFFIX, _forced_change, check=True)
_bound("account", PASSWORD_POLICY_SUFFIX, _signed_in, check=False)
_bound("account", PASSWORD_CHECK_SUFFIX, _signed_in, check=True)
