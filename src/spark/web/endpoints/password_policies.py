"""Password policy and watch list administration (server mode, ``spark.users.admin``).

SPARK's screens over warden 0.2.0's password policy stores (``Identity.password_policies``
and ``Identity.watch_lists``), in warden's own vocabulary; warden audits every change
itself, with the administrator as actor, so nothing here records audit events. The screens
use warden's Python API rather than its bearer-token routes, as every other administration
screen does: SPARK's sessions are cookies, and the permission and CSRF checks are SPARK's.

* ``/admin/password-policies``: the policies, highest priority first and the default last,
  with the roles each applies to and how many users it covers; move one up or down; map a
  role to a policy (the highest-priority policy among a user's roles applies, else the
  default). ``/admin/password-policies/new`` and ``/admin/password-policies/{id}``: every
  rule warden supports (length, character kinds, repeats, the user's own details,
  passphrases, tiers with change periods, history, minimum age), the watch lists and the
  breach range service the policy checks, and the re-check settings. The default policy
  has no priority and cannot be deleted.
* ``/admin/watch-lists``: lists of passwords that must not be used. A manual list holds
  exact passwords (hashed at once, never shown again) and contained terms; an imported list
  holds digests from a file in the Have I Been Pwned downloader format, replaced by each
  import. Nothing here shows an entry, a term or a hash.
* A user's page shows the policy that applies and why, with pin and unpin
  (``_admin_user_password_policy.html``).

warden refuses what it will not do (a clash of name or priority, the default's removal, a
list still attached, a file it cannot read) and the screen shows its reason.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from warden.identity import UserError
from warden.identity.password_policy import (
    PassphraseRule,
    PasswordPolicy,
    PasswordPolicyError,
    PasswordRules,
    PolicySettings,
    Tier,
    WatchList,
    WatchListError,
    WatchListImportError,
    WatchListKind,
)
from warden.identity.password_policy.rules import MAX_TIERS
from warden.identity.password_policy.store import MAX_HISTORY, MAX_MIN_AGE_HOURS
from warden.identity.password_policy.watchlists import MAX_TERM, MIN_TERM
from warden.identity.users import User, allows_password

from spark.identity.access import SparkIdentity, require_permission
from spark.identity.permissions import USERS_ADMIN
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import source_ip
from spark.web.endpoints.account import screen
from spark.web.endpoints.identity import identity_runtime

router = APIRouter()

POLICIES_PATH = "/admin/password-policies"
ROLES_PATH = "/admin/password-policy-roles"
LISTS_PATH = "/admin/watch-lists"
USERS_PATH = "/admin/users"
PREVIEW_PATH = "/admin/password-policy-preview"
# Exact passwords or terms in one request (warden's own bound for its routes).
MAX_ENTRIES = 100
MAX_ENTRY_LENGTH = 1024
# The largest file one import takes. warden reads it a line at a time, so memory stays small;
# the cap is the screen's, to stop a mistaken upload tying a request up.
MAX_IMPORT_BYTES = 512 * 1024 * 1024
BLANK_TIER_ROWS = 2

administrator = require_permission(USERS_ADMIN)

DONE = {
    "created": "The password policy is created.",
    "saved": "The password policy is saved.",
    "deleted": "The password policy is deleted. Its users are covered by a role mapping or the default.",
    "moved": "The priority order is saved.",
    "mapped": "The role is mapped.",
    "unmapped": "The role no longer has a policy of its own, so the default applies to it.",
    "list-created": "The watch list is created.",
    "list-saved": "The watch list is saved.",
    "list-deleted": "The watch list is deleted.",
    "added": "The passwords are added.",
    "removed": "The passwords are removed.",
    "terms-added": "The terms are added.",
    "terms-removed": "The terms are removed.",
    "imported": "The file is imported and the list now holds its hashes.",
}
SIGN_IN_ONLY_NOTE = (
    "This user does not sign in with a password, so no password policy applies to them."
)
POLICY_STALE = "The policy changed while you were editing it. Reload it and make your change again."


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _runtime(request: Request) -> IdentityRuntime:
    return identity_runtime(request)


def _actor(identity: SparkIdentity) -> str:
    return identity.warden_user_id


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404) from None


def _done(path: str, code: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?done={code}", status_code=303)


def _message(exc: Exception) -> str:
    """warden's own reason for a refusal: a fixed message with no secret in it."""
    return str(exc) or "That change was refused."


# -- Policies: reading a form -------------------------------------------------------------------


def _whole(
    form: Mapping[str, Any],
    name: str,
    label: str,
    errors: list[str],
    *,
    blank: int | None = None,
) -> int | None:
    """A whole number from a field; ``blank`` is what an empty field means (None: an error)."""
    raw = _text(form.get(name)).strip()
    if raw == "":
        if blank is None:
            errors.append(f"{label} needs a whole number.")
        return blank
    if len(raw) > 9 or not raw.isdigit():
        errors.append(f"{label} must be a whole number, 0 or more.")
        return None
    return int(raw)


def _checked(form: Mapping[str, Any], name: str) -> bool:
    return _text(form.get(name)) == "on"


def _rows(form: Any, name: str) -> list[str]:
    return [v for v in form.getlist(name) if isinstance(v, str)]


def _tiers(form: Any, errors: list[str]) -> tuple[list[Tier], list[dict[str, Any]]]:
    """The tier rows in a form: the tiers they name and the rows to show again."""
    names = _rows(form, "tier_name")
    lengths = _rows(form, "tier_min_length")
    classes = _rows(form, "tier_min_classes")
    ages = _rows(form, "tier_max_age_days")
    phrases = {v for v in _rows(form, "tier_passphrase")}
    tiers: list[Tier] = []
    shown: list[dict[str, Any]] = []
    for index, name in enumerate(names[: MAX_TIERS + BLANK_TIER_ROWS]):
        row = {
            "name": name,
            "passphrase": str(index) in phrases,
            "min_length": lengths[index] if index < len(lengths) else "",
            "min_classes": classes[index] if index < len(classes) else "",
            "max_age_days": ages[index] if index < len(ages) else "",
        }
        shown.append(row)
        if not name.strip():
            continue
        label = f"Tier {name.strip()[:32]}"
        length = _whole(row, "min_length", f"{label}: its length", errors, blank=0)
        kinds = _whole(row, "min_classes", f"{label}: its character kinds", errors, blank=0)
        age = _whole(row, "max_age_days", f"{label}: its change period", errors, blank=-1)
        if length is None or kinds is None or age is None:
            continue
        tiers.append(Tier(name.strip(), row["passphrase"], length, kinds, None if age < 0 else age))
    return tiers, shown


def _view_of(policy: PasswordPolicy) -> dict[str, Any]:
    """A stored policy as the form shows it."""
    settings = policy.settings
    rules = settings.rules
    phrase = rules.passphrase
    return {
        "id": str(policy.id),
        "version": policy.version,
        "is_default": policy.is_default,
        "priority": "" if policy.priority is None else str(policy.priority),
        "roles": list(policy.roles),
        "name": settings.name,
        "description": settings.description,
        "min_length": str(rules.min_length),
        "max_length": str(rules.max_length),
        "min_classes": str(rules.min_classes),
        "max_repeat": "" if rules.max_repeat is None else str(rules.max_repeat),
        "forbid_user_details": rules.forbid_user_details,
        "passphrase_on": phrase is not None,
        "passphrase_min_length": "" if phrase is None else str(phrase.min_length),
        "passphrase_min_words": "" if phrase is None else str(phrase.min_words),
        "passphrase_relaxes": phrase is not None and phrase.relaxes_classes,
        "tiers": [
            {
                "name": t.name,
                "passphrase": t.passphrase,
                "min_length": str(t.min_length),
                "min_classes": str(t.min_classes),
                "max_age_days": "" if t.max_age_days is None else str(t.max_age_days),
            }
            for t in rules.tiers
        ],
        "warning_days": str(rules.warning_days),
        "history_count": str(settings.history_count),
        "min_age_hours": str(settings.min_age_hours),
        "include_builtin": settings.include_builtin,
        "hibp_enabled": settings.hibp_enabled,
        "hibp_on_unavailable": settings.hibp_on_unavailable,
        "fingerprint_enabled": settings.fingerprint_enabled,
        "end_sessions_on_match": settings.end_sessions_on_match,
        "enforce_on_sign_in": settings.enforce_on_sign_in,
        "watch_list_ids": [str(i) for i in settings.watch_list_ids],
    }


def _with_blank_tiers(view: dict[str, Any]) -> dict[str, Any]:
    """The tier rows, then enough empty ones to add a tier (a row with no name is ignored)."""
    rows = list(view["tiers"])
    blank = {
        "name": "",
        "passphrase": False,
        "min_length": "",
        "min_classes": "",
        "max_age_days": "",
    }
    missing = BLANK_TIER_ROWS - sum(1 for row in rows if not row["name"].strip())
    for _ in range(max(0, min(missing, MAX_TIERS - len(rows)))):
        rows.append(dict(blank))
    return {**view, "tiers": rows}


def _parse_policy(
    form: Any, identity_policies: Any, existing: PasswordPolicy | None
) -> tuple[PolicySettings | None, int | None, list[str], dict[str, Any]]:
    """The settings in a policy form, a priority if it carries one, the problems, the view."""
    errors: list[str] = []
    is_default = existing is not None and existing.is_default
    min_length = _whole(form, "min_length", "The minimum length", errors)
    max_length = _whole(form, "max_length", "The maximum length", errors)
    min_classes = _whole(form, "min_classes", "The character kinds", errors, blank=0)
    repeat = _whole(form, "max_repeat", "The repeat limit", errors, blank=-1)
    warning = _whole(form, "warning_days", "The warning days", errors, blank=0)
    history = _whole(form, "history_count", "The history", errors, blank=0)
    age = _whole(form, "min_age_hours", "The minimum age", errors, blank=0)
    priority = None if is_default else _whole(form, "priority", "The priority", errors)
    phrase_on = _checked(form, "passphrase_on")
    phrase: PassphraseRule | None = None
    if phrase_on:
        words = _whole(form, "passphrase_min_words", "The passphrase words", errors)
        length = _whole(form, "passphrase_min_length", "The passphrase length", errors)
        if words is not None and length is not None:
            phrase = PassphraseRule(
                min_length=length,
                min_words=words,
                relaxes_classes=_checked(form, "passphrase_relaxes"),
            )
    tiers, shown = _tiers(form, errors)
    chosen = []
    for raw in _rows(form, "watch_lists"):
        try:
            chosen.append(uuid.UUID(raw))
        except ValueError:
            errors.append("A watch list in the form is not one this server holds.")
    unavailable = _text(form.get("hibp_on_unavailable"))
    view = {
        "id": "" if existing is None else str(existing.id),
        "version": _text(form.get("version")),
        "is_default": is_default,
        "priority": _text(form.get("priority")),
        "roles": [] if existing is None else list(existing.roles),
        "name": _text(form.get("name")),
        "description": _text(form.get("description")),
        "min_length": _text(form.get("min_length")),
        "max_length": _text(form.get("max_length")),
        "min_classes": _text(form.get("min_classes")),
        "max_repeat": _text(form.get("max_repeat")),
        "forbid_user_details": _checked(form, "forbid_user_details"),
        "passphrase_on": phrase_on,
        "passphrase_min_length": _text(form.get("passphrase_min_length")),
        "passphrase_min_words": _text(form.get("passphrase_min_words")),
        "passphrase_relaxes": _checked(form, "passphrase_relaxes"),
        "tiers": shown,
        "warning_days": _text(form.get("warning_days")),
        "history_count": _text(form.get("history_count")),
        "min_age_hours": _text(form.get("min_age_hours")),
        "include_builtin": _checked(form, "include_builtin"),
        "hibp_enabled": _checked(form, "hibp_enabled"),
        "hibp_on_unavailable": unavailable,
        "fingerprint_enabled": _checked(form, "fingerprint_enabled"),
        "end_sessions_on_match": _checked(form, "end_sessions_on_match"),
        "enforce_on_sign_in": _checked(form, "enforce_on_sign_in"),
        "watch_list_ids": [str(i) for i in chosen],
    }
    if errors or min_length is None or max_length is None:
        return None, priority, errors, view
    settings = PolicySettings(
        name=_text(form.get("name")).strip(),
        description=_text(form.get("description")).strip(),
        rules=PasswordRules(
            min_length=min_length,
            max_length=max_length,
            min_classes=min_classes or 0,
            max_repeat=None if repeat is None or repeat < 0 else repeat,
            forbid_user_details=_checked(form, "forbid_user_details"),
            passphrase=phrase,
            tiers=tuple(tiers),
            warning_days=warning or 0,
        ),
        history_count=history or 0,
        min_age_hours=age or 0,
        include_builtin=_checked(form, "include_builtin"),
        watch_list_ids=tuple(chosen),
        hibp_enabled=_checked(form, "hibp_enabled"),
        hibp_on_unavailable="refuse" if unavailable == "refuse" else "allow",
        fingerprint_enabled=_checked(form, "fingerprint_enabled"),
        end_sessions_on_match=_checked(form, "end_sessions_on_match"),
        enforce_on_sign_in=_checked(form, "enforce_on_sign_in"),
    )
    problems = settings.problems(fingerprint_available=identity_policies.fingerprint_available)
    found: list[str] = []
    for problem in problems:
        found.extend(part for part in problem.split("; ") if part)
    return (None if found else settings), priority, found, view


# -- Policies: pages ----------------------------------------------------------------------------


async def _lists_named(runtime: IdentityRuntime) -> list[WatchList]:
    return await runtime.identity.watch_lists.list()


async def _policies_page(
    request: Request,
    runtime: IdentityRuntime,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    policies = await runtime.identity.password_policies.list()
    covered = {
        p.id: await runtime.identity.password_policies.affected_users(p.id) for p in policies
    }
    mapping = {role: p for p in policies for role in p.roles}
    others = [p for p in policies if not p.is_default]
    return screen(
        request,
        "admin_password_policies.html",
        {
            "policies": policies,
            "covered": covered,
            "movable": [str(p.id) for p in others],
            "roles": [
                {"name": role, "policy": mapping.get(role)}
                for role in sorted(runtime.identity.password_policies.declared_roles())
            ],
            "mappable": others,
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
    )


@router.get(POLICIES_PATH, response_class=HTMLResponse)
async def policies_page(
    request: Request, _: SparkIdentity = Depends(administrator)
) -> HTMLResponse:
    return await _policies_page(request, _runtime(request))


async def _form_page(
    request: Request,
    runtime: IdentityRuntime,
    view: dict[str, Any],
    *,
    policy: PasswordPolicy | None,
    errors: list[str] | None = None,
    status_code: int = 200,
    error: str | None = None,
) -> HTMLResponse:
    lists = await _lists_named(runtime)
    covered = None
    if policy is not None:
        covered = await runtime.identity.password_policies.affected_users(policy.id)
    return screen(
        request,
        "admin_password_policy.html",
        {
            "policy": policy,
            "view": _with_blank_tiers(view),
            "watch_lists": lists,
            "covered": covered,
            "fingerprint_available": runtime.identity.password_policies.fingerprint_available,
            "problems": errors or [],
            "limits": {"history": MAX_HISTORY, "age": MAX_MIN_AGE_HOURS, "tiers": MAX_TIERS},
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
    )


async def _starting_view(runtime: IdentityRuntime) -> dict[str, Any]:
    """A new policy starts from the default's rules, at the next free priority."""
    policies = runtime.identity.password_policies
    default = await policies.default()
    taken = [p.priority for p in await policies.list() if p.priority is not None]
    view = _view_of(default)
    return {
        **view,
        "id": "",
        "version": "",
        "is_default": False,
        "roles": [],
        "name": "",
        "description": "",
        "priority": str(max(taken, default=0) + 1),
        "watch_list_ids": [],
    }


@router.get(POLICIES_PATH + "/new", response_class=HTMLResponse)
async def new_policy_page(
    request: Request, _: SparkIdentity = Depends(administrator)
) -> HTMLResponse:
    runtime = _runtime(request)
    return await _form_page(request, runtime, await _starting_view(runtime), policy=None)


async def _policy_or_404(runtime: IdentityRuntime, policy_id: str) -> PasswordPolicy:
    try:
        return await runtime.identity.password_policies.get(_uuid(policy_id))
    except PasswordPolicyError:
        raise HTTPException(status_code=404) from None


@router.post(POLICIES_PATH)
async def create_policy(
    request: Request, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    form = await request.form()
    settings, priority, problems, view = _parse_policy(
        form, runtime.identity.password_policies, None
    )
    if settings is None or priority is None:
        return await _form_page(
            request, runtime, view, policy=None, errors=problems, status_code=400
        )
    try:
        created = await runtime.identity.password_policies.create(
            settings,
            priority=priority,
            actor=_actor(identity),
            source_ip=source_ip(runtime, request),
        )
    except (PasswordPolicyError, WatchListError) as exc:
        return await _form_page(
            request, runtime, view, policy=None, error=_message(exc), status_code=409
        )
    return _done(f"{POLICIES_PATH}/{created.id}", "created")


@router.get(POLICIES_PATH + "/{policy_id}", response_class=HTMLResponse)
async def policy_page(
    request: Request, policy_id: str, _: SparkIdentity = Depends(administrator)
) -> HTMLResponse:
    runtime = _runtime(request)
    policy = await _policy_or_404(runtime, policy_id)
    return await _form_page(request, runtime, _view_of(policy), policy=policy)


@router.post(POLICIES_PATH + "/{policy_id}")
async def save_policy(
    request: Request, policy_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    policies = runtime.identity.password_policies
    policy = await _policy_or_404(runtime, policy_id)
    form = await request.form()
    settings, priority, problems, view = _parse_policy(form, policies, policy)
    if settings is None:
        return await _form_page(
            request, runtime, view, policy=policy, errors=problems, status_code=400
        )
    version = _text(form.get("version"))
    if not version.isdigit() or int(version) != policy.version:
        return await _form_page(
            request, runtime, view, policy=policy, error=POLICY_STALE, status_code=409
        )
    actor, origin = _actor(identity), source_ip(runtime, request)
    try:
        if not policy.is_default and priority is not None and priority != policy.priority:
            moved = await policies.set_priority(policy.id, priority, actor=actor, source_ip=origin)
            version = str(moved.version)
        await policies.update(
            policy.id, settings, expected_version=int(version), actor=actor, source_ip=origin
        )
    except (PasswordPolicyError, WatchListError) as exc:
        return await _form_page(
            request, runtime, view, policy=policy, error=_message(exc), status_code=409
        )
    return _done(f"{POLICIES_PATH}/{policy.id}", "saved")


@router.post(POLICIES_PATH + "/{policy_id}/delete")
async def delete_policy(
    request: Request, policy_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    policy = await _policy_or_404(runtime, policy_id)
    try:
        await runtime.identity.password_policies.delete(
            policy.id, actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except PasswordPolicyError as exc:
        return await _form_page(
            request, runtime, _view_of(policy), policy=policy, error=_message(exc), status_code=409
        )
    return _done(POLICIES_PATH, "deleted")


@router.post(POLICIES_PATH + "/{policy_id}/move")
async def move_policy(
    request: Request, policy_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Swap a policy with its neighbour in the priority order (``direction`` up or down)."""
    runtime = _runtime(request)
    policy = await _policy_or_404(runtime, policy_id)
    form = await request.form()
    direction = _text(form.get("direction"))
    ordered = [p.id for p in await runtime.identity.password_policies.list() if not p.is_default]
    if policy.is_default or direction not in ("up", "down") or policy.id not in ordered:
        return await _policies_page(
            request, runtime, error="That policy cannot be moved that way.", status_code=400
        )
    here = ordered.index(policy.id)
    there = here - 1 if direction == "up" else here + 1
    if 0 <= there < len(ordered):
        ordered[here], ordered[there] = ordered[there], ordered[here]
        try:
            await runtime.identity.password_policies.reorder(
                ordered, actor=_actor(identity), source_ip=source_ip(runtime, request)
            )
        except PasswordPolicyError as exc:
            return await _policies_page(request, runtime, error=_message(exc), status_code=409)
    return _done(POLICIES_PATH, "moved")


@router.post(ROLES_PATH + "/{role}")
async def map_role(
    request: Request, role: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Map a role to a policy, or with no policy chosen remove its mapping."""
    runtime = _runtime(request)
    form = await request.form()
    chosen = _text(form.get("policy_id")).strip()
    target: uuid.UUID | None = None
    if chosen:
        try:
            target = uuid.UUID(chosen)
        except ValueError:
            return await _policies_page(
                request, runtime, error="Choose one of the policies listed.", status_code=400
            )
    try:
        await runtime.identity.password_policies.map_role(
            role, target, actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except PasswordPolicyError as exc:
        return await _policies_page(request, runtime, error=_message(exc), status_code=409)
    return _done(POLICIES_PATH, "mapped" if target is not None else "unmapped")


# -- Watch lists --------------------------------------------------------------------------------


def _attached(policies: list[PasswordPolicy], listed: WatchList) -> list[PasswordPolicy]:
    return [p for p in policies if listed.id in p.settings.watch_list_ids]


async def _lists_page(
    request: Request,
    runtime: IdentityRuntime,
    *,
    error: str | None = None,
    status_code: int = 200,
    name: str = "",
    reason: str = "",
) -> HTMLResponse:
    policies = await runtime.identity.password_policies.list()
    lists = await _lists_named(runtime)
    return screen(
        request,
        "admin_watch_lists.html",
        {
            "lists": [{"list": w, "policies": _attached(policies, w)} for w in lists],
            "policies": policies,
            "range_users": [p for p in policies if p.settings.hibp_enabled],
            "form": {"name": name, "reason": reason},
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
    )


@router.get(LISTS_PATH, response_class=HTMLResponse)
async def lists_page(request: Request, _: SparkIdentity = Depends(administrator)) -> HTMLResponse:
    return await _lists_page(request, _runtime(request))


@router.post(LISTS_PATH)
async def create_list(
    request: Request, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    form = await request.form()
    name, reason = _text(form.get("name")).strip(), _text(form.get("reason")).strip()
    kind = _text(form.get("kind"))
    try:
        created = await runtime.identity.watch_lists.create(
            name,
            reason,
            (
                WatchListKind.IMPORTED
                if kind == WatchListKind.IMPORTED.value
                else WatchListKind.MANUAL
            ),
            actor=_actor(identity),
            source_ip=source_ip(runtime, request),
        )
    except WatchListError as exc:
        return await _lists_page(
            request, runtime, error=_message(exc), status_code=400, name=name, reason=reason
        )
    return _done(f"{LISTS_PATH}/{created.id}", "list-created")


async def _list_or_404(runtime: IdentityRuntime, list_id: str) -> WatchList:
    try:
        return await runtime.identity.watch_lists.get(_uuid(list_id))
    except WatchListError:
        raise HTTPException(status_code=404) from None


async def _list_page(
    request: Request,
    runtime: IdentityRuntime,
    listed: WatchList,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    policies = await runtime.identity.password_policies.list()
    return screen(
        request,
        "admin_watch_list.html",
        {
            "listed": listed,
            "imported": listed.kind is WatchListKind.IMPORTED,
            "policies": _attached(policies, listed),
            "max_entries": MAX_ENTRIES,
            "term_range": (MIN_TERM, MAX_TERM),
            "max_import_mib": MAX_IMPORT_BYTES // (1024 * 1024),
            "notice": DONE.get(request.query_params.get("done", "")),
            "error": error,
        },
        status_code=status_code,
    )


@router.get(LISTS_PATH + "/{list_id}", response_class=HTMLResponse)
async def list_page(
    request: Request, list_id: str, _: SparkIdentity = Depends(administrator)
) -> HTMLResponse:
    runtime = _runtime(request)
    return await _list_page(request, runtime, await _list_or_404(runtime, list_id))


@router.post(LISTS_PATH + "/{list_id}")
async def save_list(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    listed = await _list_or_404(runtime, list_id)
    form = await request.form()
    try:
        await runtime.identity.watch_lists.update(
            listed.id,
            name=_text(form.get("name")).strip(),
            reason=_text(form.get("reason")).strip(),
            actor=_actor(identity),
            source_ip=source_ip(runtime, request),
        )
    except WatchListError as exc:
        return await _list_page(request, runtime, listed, error=_message(exc), status_code=400)
    return _done(f"{LISTS_PATH}/{listed.id}", "list-saved")


@router.post(LISTS_PATH + "/{list_id}/delete")
async def delete_list(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    runtime = _runtime(request)
    listed = await _list_or_404(runtime, list_id)
    try:
        await runtime.identity.watch_lists.delete(
            listed.id, actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except WatchListError as exc:
        return await _list_page(request, runtime, listed, error=_message(exc), status_code=409)
    return _done(LISTS_PATH, "list-deleted")


def _entries(form: Any, name: str) -> list[str] | None:
    """The lines of a textarea, blank ones dropped; None when there are none or too many."""
    lines = [line.rstrip("\r") for line in _text(form.get(name)).split("\n")]
    lines = [line for line in lines if line.strip()]
    if not 1 <= len(lines) <= MAX_ENTRIES or any(len(line) > MAX_ENTRY_LENGTH for line in lines):
        return None
    return lines


async def _change_entries(
    request: Request,
    list_id: str,
    identity: SparkIdentity,
    *,
    field: str,
    removing: bool,
    done: str,
) -> Response:
    """Add or remove passwords (``field`` entries) or terms; nothing entered is shown again."""
    runtime = _runtime(request)
    lists = runtime.identity.watch_lists
    listed = await _list_or_404(runtime, list_id)
    form = await request.form()
    lines = _entries(form, field)
    if lines is None:
        return await _list_page(
            request,
            runtime,
            listed,
            error=f"Enter between 1 and {MAX_ENTRIES} lines, one per line, each up to {MAX_ENTRY_LENGTH} characters.",
            status_code=400,
        )
    actor, origin = _actor(identity), source_ip(runtime, request)
    try:
        if field == "entries":
            change = lists.remove_entries if removing else lists.add_entries
        else:
            change = lists.remove_terms if removing else lists.add_terms
        await change(listed.id, lines, actor=actor, source_ip=origin)
    except WatchListError as exc:
        return await _list_page(request, runtime, listed, error=_message(exc), status_code=400)
    return _done(f"{LISTS_PATH}/{listed.id}", done)


@router.post(LISTS_PATH + "/{list_id}/entries")
async def add_entries(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    return await _change_entries(
        request, list_id, identity, field="entries", removing=False, done="added"
    )


@router.post(LISTS_PATH + "/{list_id}/entries/remove")
async def remove_entries(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    return await _change_entries(
        request, list_id, identity, field="entries", removing=True, done="removed"
    )


@router.post(LISTS_PATH + "/{list_id}/terms")
async def add_terms(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    return await _change_entries(
        request, list_id, identity, field="terms", removing=False, done="terms-added"
    )


@router.post(LISTS_PATH + "/{list_id}/terms/remove")
async def remove_terms(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    return await _change_entries(
        request, list_id, identity, field="terms", removing=True, done="terms-removed"
    )


async def _body_lines(request: Request) -> AsyncIterator[bytes]:
    """The request body a line at a time, refusing a file over :data:`MAX_IMPORT_BYTES`."""
    total = 0
    pending = b""
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_IMPORT_BYTES:
            raise WatchListImportError(
                f"the file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB"
            )
        *whole, pending = (pending + chunk).split(b"\n")
        for line in whole:
            yield line
    if pending:
        yield pending


@router.post(LISTS_PATH + "/{list_id}/import")
async def import_list(
    request: Request, list_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Replace an imported list's hashes from the request body (a file in HIBP's format).

    The page sends the chosen file as the raw body (``password_admin.js``), so a large file
    streams to warden a line at a time. An optional ``threshold`` query keeps only hashes
    seen at least that many times. The answer is JSON: ``ok`` and a ``message``.
    """
    runtime = _runtime(request)
    listed = await _list_or_404(runtime, list_id)
    raw = request.query_params.get("threshold", "").strip()
    if raw and (len(raw) > 9 or not raw.isdigit() or int(raw) < 1):
        return JSONResponse(
            {"ok": False, "message": "The threshold must be a whole number of at least 1."},
            status_code=400,
        )
    try:
        result = await runtime.identity.watch_lists.import_hibp(
            listed.id,
            _body_lines(request),
            threshold=int(raw) if raw else None,
            actor=_actor(identity),
            source_ip=source_ip(runtime, request),
        )
    except WatchListError as exc:
        return JSONResponse({"ok": False, "message": _message(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "message": DONE["imported"],
            "imported": result.imported,
            "below_threshold": result.below_threshold,
            "lines": result.lines,
        }
    )


# -- A user's policy ----------------------------------------------------------------------------


async def user_context(runtime: IdentityRuntime, user: User) -> dict[str, Any]:
    """What ``_admin_user_password_policy.html`` shows for a user."""
    if not allows_password(user.sign_in_method):
        return {"password_policy": {"applies": False, "note": SIGN_IN_ONLY_NOTE}}
    identity = runtime.identity
    effective = await identity.password_policies.effective(user.id)
    state = await identity.password_states.get(user.id)
    policies = await identity.password_policies.list()
    return {
        "password_policy": {
            "applies": True,
            "policy": effective.policy,
            "source": effective.source,
            "role": effective.role,
            "tier": None if state is None else state.tier,
            "expires_at": None if state is None else state.expires_at,
            "change_reason": None if state is None else state.change_reason,
            "choices": policies,
        }
    }


@router.post(USERS_PATH + "/{user_id}/password-policy")
async def pin_policy(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Pin a policy on one user, over any role mapping."""
    from spark.web.endpoints import administration

    runtime = _runtime(request)
    user = await administration._user_or_404(runtime, user_id)
    form = await request.form()
    try:
        target = uuid.UUID(_text(form.get("policy_id")))
    except ValueError:
        return await administration._user_page(
            request, runtime, user, error="Choose one of the policies listed.", status_code=400
        )
    if not allows_password(user.sign_in_method):
        return await administration._user_page(
            request, runtime, user, error=SIGN_IN_ONLY_NOTE, status_code=409
        )
    try:
        await runtime.identity.password_policies.pin(
            user.id, target, actor=_actor(identity), source_ip=source_ip(runtime, request)
        )
    except (PasswordPolicyError, UserError) as exc:
        return await administration._user_page(
            request, runtime, user, error=_message(exc), status_code=409
        )
    return _done(f"{USERS_PATH}/{user.id}", "policy-pinned")


@router.post(USERS_PATH + "/{user_id}/password-policy/unpin")
async def unpin_policy(
    request: Request, user_id: str, identity: SparkIdentity = Depends(administrator)
) -> Response:
    """Take a user's pin off, so their roles (or the default) choose the policy again."""
    from spark.web.endpoints import administration

    runtime = _runtime(request)
    user = await administration._user_or_404(runtime, user_id)
    await runtime.identity.password_policies.unpin(
        user.id, actor=_actor(identity), source_ip=source_ip(runtime, request)
    )
    return _done(f"{USERS_PATH}/{user.id}", "policy-unpinned")


@router.post(PREVIEW_PATH)
async def preview_policy(
    request: Request, _: SparkIdentity = Depends(administrator)
) -> JSONResponse:
    """The rules a password typed for a new user would meet, for the roles ticked on the form.

    The Create user form has no credential of the new person to bind warden's description
    routes to, so this describes the policy the roles would resolve to (the default when none
    maps), for the browser's checklist only. warden still enforces the policy on submit and the
    refusal is shown. Nothing is stored and no password is sent here.
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    roles = body.get("roles") if isinstance(body, dict) else None
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        return JSONResponse({"detail": "Send the roles as a list."}, status_code=400)
    enforcement = _runtime(request).identity.password_enforcement
    policy = await enforcement.policy_for_roles(roles)
    return JSONResponse(
        enforcement.advice(policy).describe(), headers={"Cache-Control": "no-store"}
    )
