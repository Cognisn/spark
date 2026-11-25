"""Password policies, watch lists and live feedback on SPARK's screens (server mode, warden 0.2.0).

The real app on a PostgreSQL clone at head with synthetic users.

* Administration (``spark.users.admin``): ``/admin/password-policies`` (list, create, edit with
  every rule warden supports, delete, priority order, role mapping) and ``/admin/watch-lists``
  (lists, passwords and terms, an import of hashes, the breach range service), one navigation
  entry. A user's page shows the policy that applies and why, with pin and unpin.
* Live feedback (CS23): warden's packaged ``passwordrules.js`` is served from the installed
  package (never copied into the repository) and every page that sets a password carries the
  checklist, fed by the policy warden describes for that place.

Every route is checked for its permission and its CSRF token.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
import warden.static
from fastapi.testclient import TestClient
from warden.identity import ChangeReason
from warden.identity.password_policy import (
    PasswordRules,
    PolicySettings,
    WatchListKind,
)

from spark.identity.permissions import ADMINISTRATOR, POWER_USER, USER
from spark.web.security import CSRF_HEADER
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    ORIGIN,
    field,
    form_token,
    invite,
    link_from,
    message_of,
    mfa_client,
    outbox,
    plain_client,
    post_form,
    server_db,
)
from tests.identity_web_helpers import (
    PASSWORD,
    PUBLIC,
    another_client,
    make_user,
    runtime,
    sign_in,
)

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "admin@example.com"
ADA = "ada@example.com"
GRACE = "grace@example.com"
LIN = "lin@example.com"
POLICIES = "/admin/password-policies"
LISTS = "/admin/watch-lists"
RULES_PATH = "/auth/password-rules.js"
STRONG = "a-fresh-long-password-dev-only"


# -- The evaluator: no copy in the repository (no database needed) ---------------------------


def test_no_copy_of_the_evaluator_is_in_the_repository() -> None:
    skipped = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".mypy_cache",
    }
    found = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("passwordrules*")
        if not skipped & set(path.relative_to(ROOT).parts)
    ]
    assert found == []


def test_the_package_ships_the_evaluator() -> None:
    assert warden.static.path("passwordrules.js").is_file()


@pytest.fixture
def admin(plain_client: TestClient) -> TestClient:
    make_user(plain_client, ADMIN, ADMINISTRATOR)
    assert sign_in(plain_client, ADMIN).status_code == 303
    return plain_client


def _identity(client: TestClient) -> Any:
    return runtime(client).identity


def _call(client: TestClient, fn: Any) -> Any:
    return client.portal.call(fn)  # type: ignore[attr-defined]


def _page(client: TestClient, path: str) -> Any:
    page = client.get(path, headers=NAVIGATE, follow_redirects=False)
    assert page.status_code == 200, (path, page.status_code, page.text[:300])
    return page


def _text(page: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.text))


def _policy_form(name: str, priority: int | None = 1, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "description": "A synthetic policy",
        "min_length": "12",
        "max_length": "128",
        "min_classes": "0",
        "max_repeat": "",
        "warning_days": "14",
        "history_count": "0",
        "min_age_hours": "0",
        "include_builtin": "on",
        "hibp_on_unavailable": "allow",
        "tier_name": ["", ""],
        "tier_min_length": ["", ""],
        "tier_min_classes": ["", ""],
        "tier_max_age_days": ["", ""],
    }
    if priority is not None:
        data["priority"] = str(priority)
    data.update(over)
    return data


def _create(client: TestClient, name: str, priority: int, **over: Any) -> uuid.UUID:
    resp = client.post(POLICIES, data=_policy_form(name, priority, **over), follow_redirects=False)
    assert resp.status_code == 303, resp.text[:400]
    match = re.search(r"/admin/password-policies/([0-9a-f-]{36})", resp.headers["location"])
    assert match
    return uuid.UUID(match.group(1))


def _policies(client: TestClient) -> list[Any]:
    return _call(client, _identity(client).password_policies.list)  # type: ignore[no-any-return]


def _named(client: TestClient, name: str) -> Any:
    return next(p for p in _policies(client) if p.settings.name == name)


def _effective(client: TestClient, user_id: uuid.UUID) -> Any:
    return _call(client, lambda: _identity(client).password_policies.effective(user_id))


def _make_list(client: TestClient, name: str, kind: str = "manual") -> uuid.UUID:
    resp = client.post(
        LISTS,
        data={"name": name, "reason": f"{name} is not allowed", "kind": kind},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:300]
    return uuid.UUID(resp.headers["location"].split("?")[0].rsplit("/", 1)[1])


def _list(client: TestClient, list_id: uuid.UUID) -> Any:
    return _call(client, lambda: _identity(client).watch_lists.get(list_id))


# -- Access ---------------------------------------------------------------------------------


@pytest.mark.postgres
class TestAccess:
    def _routes(
        self, policy: uuid.UUID, listed: uuid.UUID, user: uuid.UUID
    ) -> list[tuple[str, str]]:
        p, w, u = (
            f"{POLICIES}/{policy}",
            f"{LISTS}/{listed}",
            f"/admin/users/{user}/password-policy",
        )
        return [
            ("GET", POLICIES),
            ("GET", POLICIES + "/new"),
            ("POST", POLICIES),
            ("GET", p),
            ("POST", p),
            ("POST", p + "/delete"),
            ("POST", p + "/move"),
            ("POST", "/admin/password-policy-roles/user"),
            ("GET", LISTS),
            ("POST", LISTS),
            ("GET", w),
            ("POST", w),
            ("POST", w + "/delete"),
            ("POST", w + "/entries"),
            ("POST", w + "/entries/remove"),
            ("POST", w + "/terms"),
            ("POST", w + "/terms/remove"),
            ("POST", w + "/import"),
            ("POST", u),
            ("POST", u + "/unpin"),
        ]

    def test_another_role_is_refused_everywhere(self, admin: TestClient) -> None:
        policy = _create(admin, "Staff", 1)
        listed = _make_list(admin, "Common")
        ada = make_user(admin, ADA, USER)
        other = another_client(admin)
        assert sign_in(other, ADA).status_code == 303
        for method, path in self._routes(policy, listed, ada):
            resp = other.request(method, path, headers=NAVIGATE, follow_redirects=False)
            assert resp.status_code == 403, (method, path, resp.status_code)

    def test_a_refused_post_changes_nothing(self, admin: TestClient) -> None:
        make_user(admin, ADA, POWER_USER)
        other = another_client(admin)
        sign_in(other, ADA)
        before = len(_policies(admin))
        assert other.post(POLICIES, data=_policy_form("Sneaky")).status_code == 403
        assert other.post(LISTS, data={"name": "x", "reason": "y"}).status_code == 403
        assert len(_policies(admin)) == before
        assert _call(admin, _identity(admin).watch_lists.list) == []

    def test_signed_out_goes_to_sign_in(self, plain_client: TestClient) -> None:
        for path in (POLICIES, LISTS):
            resp = plain_client.get(path, headers=NAVIGATE, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/sign-in")

    def test_the_routes_need_the_csrf_token(self, admin: TestClient) -> None:
        policy = _create(admin, "Staff", 1)
        listed = _make_list(admin, "Common")
        ada = make_user(admin, ADA, USER)
        routes = self._routes(policy, listed, ada)
        del admin.headers[CSRF_HEADER]
        for method, path in routes:
            if method == "GET":
                continue
            resp = admin.post(path, data=_policy_form("Without a token"))
            assert resp.status_code == 403, (path, resp.status_code)
        assert [p.settings.name for p in _policies(admin) if not p.is_default] == ["Staff"]
        assert len(_call(admin, _identity(admin).watch_lists.list)) == 1

    def test_the_menu_has_one_entry_for_both_pages(self, admin: TestClient) -> None:
        menu = _page(admin, "/admin").text
        assert 'href="/admin/password-policies"' in menu
        assert 'href="/admin/watch-lists"' not in menu
        for path in (POLICIES, LISTS):
            tabs = _page(admin, path).text
            assert 'href="/admin/password-policies"' in tabs and 'href="/admin/watch-lists"' in tabs


# -- Policies -------------------------------------------------------------------------------


@pytest.mark.postgres
class TestPolicies:
    def test_the_default_is_listed_and_cannot_be_deleted(self, admin: TestClient) -> None:
        page = _page(admin, POLICIES)
        assert "default" in page.text and "Default" in page.text
        default = next(p for p in _policies(admin) if p.is_default)
        refused = admin.post(f"{POLICIES}/{default.id}/delete", follow_redirects=False)
        assert refused.status_code == 409
        assert any(p.is_default for p in _policies(admin))

    def test_a_policy_is_created_with_every_rule(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Company terms")
        form = _policy_form(
            "Staff",
            3,
            min_length="14",
            max_length="100",
            min_classes="3",
            max_repeat="3",
            forbid_user_details="on",
            passphrase_on="on",
            passphrase_min_words="4",
            passphrase_min_length="20",
            passphrase_relaxes="on",
            tier_name=["Strong", "Basic"],
            tier_passphrase=["0"],
            tier_min_length=["20", "14"],
            tier_min_classes=["0", "0"],
            tier_max_age_days=["", "180"],
            warning_days="21",
            history_count="5",
            min_age_hours="12",
            hibp_enabled="on",
            hibp_on_unavailable="refuse",
            enforce_on_sign_in="on",
            end_sessions_on_match="on",
            watch_lists=[str(listed)],
        )
        resp = admin.post(POLICIES, data=form, follow_redirects=False)
        assert resp.status_code == 303 and "done=created" in resp.headers["location"]
        policy = _named(admin, "Staff")
        rules = policy.settings.rules
        assert policy.priority == 3
        assert (rules.min_length, rules.max_length, rules.min_classes, rules.max_repeat) == (
            14,
            100,
            3,
            3,
        )
        assert rules.forbid_user_details is True
        assert (rules.passphrase.min_words, rules.passphrase.min_length) == (4, 20)
        assert rules.passphrase.relaxes_classes is True
        assert [(t.name, t.passphrase, t.min_length, t.max_age_days) for t in rules.tiers] == [
            ("Strong", True, 20, None),
            ("Basic", False, 14, 180),
        ]
        s = policy.settings
        assert (s.history_count, s.min_age_hours, rules.warning_days) == (5, 12, 21)
        assert s.hibp_enabled and s.hibp_on_unavailable == "refuse"
        assert s.enforce_on_sign_in and s.end_sessions_on_match
        assert s.watch_list_ids == (listed,)
        page = _page(admin, f"{POLICIES}/{policy.id}")
        assert (
            'name="name" class="form-control form-control-sm" maxlength="64" value="Staff"'
            in page.text
        )
        assert 'value="Strong"' in page.text and 'value="180"' in page.text

    def test_a_policy_warden_refuses_is_not_saved_and_says_why(self, admin: TestClient) -> None:
        before = len(_policies(admin))
        resp = admin.post(POLICIES, data=_policy_form("Weak", 1, min_length="3"))
        assert resp.status_code == 400
        assert 'id="policy-problems"' in resp.text and "min_length" in _text(resp)
        assert len(_policies(admin)) == before
        # What the administrator typed is still there.
        assert 'value="Weak"' in resp.text
        bad = admin.post(POLICIES, data=_policy_form("Words", 1, min_length="many"))
        assert bad.status_code == 400 and "whole number" in _text(bad)

    def test_a_name_or_priority_in_use_is_refused(self, admin: TestClient) -> None:
        _create(admin, "Staff", 1)
        same_name = admin.post(POLICIES, data=_policy_form("Staff", 2))
        same_priority = admin.post(POLICIES, data=_policy_form("Other", 1))
        assert same_name.status_code == 409 and same_priority.status_code == 409
        assert "already in use" in _text(same_name)

    def test_edit_changes_the_policy_and_a_stale_form_is_refused(self, admin: TestClient) -> None:
        policy_id = _create(admin, "Staff", 1)
        policy = _named(admin, "Staff")
        edit = _policy_form("Staff", 1, min_length="16", version=str(policy.version))
        edit["description"] = "Edited"
        resp = admin.post(f"{POLICIES}/{policy_id}", data=edit, follow_redirects=False)
        assert resp.status_code == 303
        assert _named(admin, "Staff").settings.rules.min_length == 16
        stale = admin.post(f"{POLICIES}/{policy_id}", data=edit)
        assert stale.status_code == 409 and "changed while you were editing" in _text(stale)
        assert _named(admin, "Staff").settings.rules.min_length == 16

    def test_the_default_is_edited_without_a_priority(self, admin: TestClient) -> None:
        default = next(p for p in _policies(admin) if p.is_default)
        form = _policy_form(
            default.settings.name, None, min_length="15", version=str(default.version)
        )
        resp = admin.post(f"{POLICIES}/{default.id}", data=form, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:300]
        assert next(p for p in _policies(admin) if p.is_default).settings.rules.min_length == 15

    def test_a_policy_is_deleted_and_its_users_return_to_the_default(
        self, admin: TestClient
    ) -> None:
        policy_id = _create(admin, "Staff", 1)
        ada = make_user(admin, ADA, USER)
        admin.post("/admin/password-policy-roles/user", data={"policy_id": str(policy_id)})
        assert _effective(admin, ada).policy.id == policy_id
        resp = admin.post(f"{POLICIES}/{policy_id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert _effective(admin, ada).source == "default"

    def test_priority_moves_with_the_arrows(self, admin: TestClient) -> None:
        first, second = _create(admin, "First", 1), _create(admin, "Second", 2)
        resp = admin.post(
            f"{POLICIES}/{second}/move", data={"direction": "up"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert (_named(admin, "Second").priority, _named(admin, "First").priority) == (1, 2)
        assert (
            admin.post(f"{POLICIES}/{first}/move", data={"direction": "sideways"}).status_code
            == 400
        )
        # The top one cannot go higher, and nothing breaks.
        assert admin.post(f"{POLICIES}/{second}/move", data={"direction": "up"}).status_code in (
            200,
            303,
        )
        assert _named(admin, "Second").priority == 1

    def test_roles_map_by_priority(self, admin: TestClient) -> None:
        senior, junior = _create(admin, "Senior", 1), _create(admin, "Junior", 2)
        lin = make_user(admin, LIN, USER, POWER_USER)
        # A role with no mapping follows the default.
        assert _effective(admin, lin).source == "default"
        admin.post("/admin/password-policy-roles/user", data={"policy_id": str(junior)})
        admin.post("/admin/password-policy-roles/power_user", data={"policy_id": str(senior)})
        found = _effective(admin, lin)
        assert (found.policy.id, found.source, found.role) == (senior, "role", "power_user")
        # Putting the other policy above it changes who wins.
        admin.post(f"{POLICIES}/{junior}/move", data={"direction": "up"})
        found = _effective(admin, lin)
        assert (found.policy.id, found.role) == (junior, "user")
        page = _page(admin, POLICIES)
        assert "Junior (priority 1)" in page.text
        # No policy chosen removes the mapping.
        admin.post("/admin/password-policy-roles/user", data={"policy_id": ""})
        assert _effective(admin, lin).role == "power_user"
        refused = admin.post("/admin/password-policy-roles/user", data={"policy_id": "not-an-id"})
        assert refused.status_code == 400

    def test_an_unknown_role_is_refused(self, admin: TestClient) -> None:
        policy = _create(admin, "Staff", 1)
        resp = admin.post(
            "/admin/password-policy-roles/no_such_role", data={"policy_id": str(policy)}
        )
        assert resp.status_code == 409 and "no such role" in _text(resp)

    def test_the_tables_sit_in_the_shared_wrapper(self, admin: TestClient) -> None:
        _create(admin, "Staff", 1)
        _make_list(admin, "Common")
        for path in (POLICIES, LISTS):
            html = _page(admin, path).text
            assert html.count("<table") == html.count('class="app-table-responsive"') >= 1
        policy = _named(admin, "Staff")
        assert 'class="app-table-responsive"' in _page(admin, f"{POLICIES}/{policy.id}").text


# -- A user's policy ------------------------------------------------------------------------


@pytest.mark.postgres
class TestUserPolicy:
    def test_the_page_says_which_policy_applies_and_why(self, admin: TestClient) -> None:
        policy = _create(admin, "Staff", 1)
        ada = make_user(admin, ADA, USER)
        page = _page(admin, f"/admin/users/{ada}")
        assert "Password policy" in page.text
        why = re.search(r'id="password-policy-why">(.*?)</span>', page.text, re.S)
        assert why and "default applies" in why.group(1)
        admin.post("/admin/password-policy-roles/user", data={"policy_id": str(policy)})
        page = _page(admin, f"/admin/users/{ada}")
        assert "the user role maps to it" in page.text and ">Staff</a>" in page.text

    def test_pin_and_unpin(self, admin: TestClient) -> None:
        mapped, pinned = _create(admin, "Mapped", 1), _create(admin, "Pinned", 2)
        admin.post("/admin/password-policy-roles/user", data={"policy_id": str(mapped)})
        ada = make_user(admin, ADA, USER)
        resp = admin.post(
            f"/admin/users/{ada}/password-policy",
            data={"policy_id": str(pinned)},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and "done=policy-pinned" in resp.headers["location"]
        assert (_effective(admin, ada).policy.id, _effective(admin, ada).source) == (pinned, "pin")
        page = _page(admin, f"/admin/users/{ada}")
        assert "pinned on this user" in page.text and "Unpin" in page.text
        # A pin outlasts the role mapping, and a policy under a pin cannot be deleted.
        assert admin.post(f"{POLICIES}/{pinned}/delete").status_code == 409
        resp = admin.post(f"/admin/users/{ada}/password-policy/unpin", follow_redirects=False)
        assert resp.status_code == 303
        assert (_effective(admin, ada).policy.id, _effective(admin, ada).source) == (mapped, "role")
        assert "Unpin" not in _page(admin, f"/admin/users/{ada}").text

    def test_a_bad_policy_id_is_refused(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        resp = admin.post(f"/admin/users/{ada}/password-policy", data={"policy_id": "x"})
        assert resp.status_code == 400
        gone = admin.post(
            f"/admin/users/{ada}/password-policy", data={"policy_id": str(uuid.uuid4())}
        )
        assert gone.status_code == 409
        assert _effective(admin, ada).source == "default"

    def test_a_user_who_does_not_sign_in_with_a_password_is_told_so(
        self, admin: TestClient
    ) -> None:
        from warden.identity import SignInMethod

        async def create() -> uuid.UUID:
            user = await _identity(admin).users.create(
                GRACE, actor="test", sign_in_method=SignInMethod.SSO_ONLY
            )
            return user.id

        grace = _call(admin, create)
        page = _page(admin, f"/admin/users/{grace}")
        assert (
            'id="password-policy-none"' in page.text
            and 'id="password-policy-name"' not in page.text
        )
        policy = _create(admin, "Staff", 1)
        refused = admin.post(
            f"/admin/users/{grace}/password-policy", data={"policy_id": str(policy)}
        )
        assert refused.status_code == 409 and "does not sign in with a password" in _text(refused)


# -- Watch lists ----------------------------------------------------------------------------


@pytest.mark.postgres
class TestWatchLists:
    def test_a_list_is_created_and_shown(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Company names")
        page = _page(admin, LISTS)
        assert "Company names" in page.text and "typed in" in page.text
        assert f"/admin/watch-lists/{listed}" in page.text
        assert "Company names is not allowed" in page.text

    def test_a_list_warden_refuses_says_why(self, admin: TestClient) -> None:
        _make_list(admin, "Same")
        resp = admin.post(LISTS, data={"name": "Same", "reason": "again", "kind": "manual"})
        assert resp.status_code == 400 and "already exists" in _text(resp)
        blank = admin.post(LISTS, data={"name": "", "reason": "r", "kind": "manual"})
        assert blank.status_code == 400

    def test_passwords_and_terms_are_added_and_never_shown(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Mine")
        secret = "synthetic-listed-password-dev-only"
        resp = admin.post(
            f"{LISTS}/{listed}/entries",
            data={"entries": f"{secret}\nanother-synthetic-one-dev-only\n"},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and "done=added" in resp.headers["location"]
        assert _list(admin, listed).entry_count == 2
        admin.post(f"{LISTS}/{listed}/terms", data={"terms": "Acme\nWidgets"})
        assert len(_list(admin, listed).terms) == 2
        page = _page(admin, f"{LISTS}/{listed}")
        assert secret not in page.text and "acme" not in page.text.lower().replace("Acme", "")
        removed = admin.post(
            f"{LISTS}/{listed}/entries/remove", data={"entries": secret}, follow_redirects=False
        )
        assert removed.status_code == 303 and _list(admin, listed).entry_count == 1
        admin.post(f"{LISTS}/{listed}/terms/remove", data={"terms": "acme"})
        assert len(_list(admin, listed).terms) == 1

    def test_a_refused_entry_is_not_echoed(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Mine")
        resp = admin.post(f"{LISTS}/{listed}/terms", data={"terms": "ab"})
        assert resp.status_code == 400 and "3 to 64" in _text(resp) and ">ab<" not in resp.text
        empty = admin.post(f"{LISTS}/{listed}/entries", data={"entries": "\n \n"})
        assert empty.status_code == 400
        many = admin.post(
            f"{LISTS}/{listed}/entries",
            data={"entries": "\n".join(f"pw-{i}-dev-only" for i in range(101))},
        )
        assert many.status_code == 400 and "between 1 and 100" in _text(many)
        assert _list(admin, listed).entry_count == 0

    def test_a_list_is_renamed_and_deleted_only_when_unused(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Mine")
        admin.post(f"{LISTS}/{listed}", data={"name": "Renamed", "reason": "A new reason"})
        assert (_list(admin, listed).name, _list(admin, listed).reason) == (
            "Renamed",
            "A new reason",
        )
        _create(admin, "Staff", 1, watch_lists=[str(listed)])
        assert listed in _named(admin, "Staff").settings.watch_list_ids
        assert "Staff" in _page(admin, LISTS).text
        refused = admin.post(f"{LISTS}/{listed}/delete")
        assert refused.status_code == 409 and _list(admin, listed)
        policy = _named(admin, "Staff")
        admin.post(
            f"{POLICIES}/{policy.id}",
            data=_policy_form("Staff", 1, version=str(policy.version)),
        )
        gone = admin.post(f"{LISTS}/{listed}/delete", follow_redirects=False)
        assert gone.status_code == 303
        assert admin.get(f"{LISTS}/{listed}", headers=NAVIGATE).status_code == 404

    def test_an_import_replaces_the_hashes(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Breached", "imported")
        page = _page(admin, f"{LISTS}/{listed}")
        assert 'data-import="/admin/watch-lists/' in page.text and "never imported" not in page.text
        body = "\n".join(
            [
                "5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8:3861493",
                "7C4A8D09CA3762AF61E59520943DC26494F8941B:20",
            ]
        )
        resp = admin.post(
            f"{LISTS}/{listed}/import",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert (
            resp.status_code == 200 and resp.json()["ok"] is True and resp.json()["imported"] == 2
        )
        assert _list(admin, listed).entry_count == 2 and _list(admin, listed).last_imported_at
        again = admin.post(f"{LISTS}/{listed}/import?threshold=100", content=body)
        assert again.json()["imported"] == 1 and again.json()["below_threshold"] == 1
        assert _list(admin, listed).import_threshold == 100

    def test_an_import_warden_refuses_shows_its_reason_and_changes_nothing(
        self, admin: TestClient
    ) -> None:
        listed = _make_list(admin, "Breached", "imported")
        good = "5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8:3861493"
        admin.post(f"{LISTS}/{listed}/import", content=good)
        bad = admin.post(f"{LISTS}/{listed}/import", content=good + "\nnot a hash at all\n")
        assert bad.status_code == 400 and bad.json()["ok"] is False
        assert (
            "HIBP's downloader format" in bad.json()["message"]
            and "line 2" in bad.json()["message"]
        )
        assert "not a hash" not in bad.text
        assert _list(admin, listed).entry_count == 1
        no_count = admin.post(
            f"{LISTS}/{listed}/import?threshold=5",
            content="5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8",
        )
        assert (
            no_count.status_code == 400 and "threshold needs a count" in no_count.json()["message"]
        )
        assert admin.post(f"{LISTS}/{listed}/import?threshold=0", content=good).status_code == 400
        assert _list(admin, listed).entry_count == 1

    def test_a_manual_list_takes_no_import(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Typed")
        resp = admin.post(
            f"{LISTS}/{listed}/import", content="5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8"
        )
        assert resp.status_code == 400 and "only an imported" in resp.json()["message"]
        assert 'id="import-form"' not in _page(admin, f"{LISTS}/{listed}").text

    def test_the_range_service_is_listed_by_policy(self, admin: TestClient) -> None:
        _create(admin, "Asks", 1, hibp_enabled="on")
        _create(admin, "Quiet", 2)
        section = _page(admin, LISTS).text.split('id="range-service"', 1)[1]
        assert "Asks" in section and "Quiet" not in section

    def test_an_unknown_list_is_not_found(self, admin: TestClient) -> None:
        assert admin.get(f"{LISTS}/{uuid.uuid4()}", headers=NAVIGATE).status_code == 404
        assert admin.get(f"{LISTS}/not-an-id", headers=NAVIGATE).status_code == 404


# -- Live feedback --------------------------------------------------------------------------


def _json(client: TestClient, path: str, page: Any, **body: Any) -> Any:
    """A JSON post from a page, with the page's own form token, as its script sends it."""
    return client.post(
        path,
        json=body,
        headers={**ORIGIN, CSRF_HEADER: form_token(page)},
    )


@pytest.mark.postgres
class TestEvaluatorRoute:
    def test_it_is_served_from_the_installed_package(self, plain_client: TestClient) -> None:
        resp = plain_client.get(RULES_PATH)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/javascript")
        assert resp.content == warden.static.path("passwordrules.js").read_bytes()
        assert b"WardenPasswordRules" in resp.content

    def test_it_needs_no_session(self, plain_client: TestClient) -> None:
        assert another_client(plain_client).get(RULES_PATH).status_code == 200


PLACES = {
    "invitation": "/auth/invitation",
    "reset": "/auth/reset",
    "challenge": "/auth/challenge",
    "account": "/account",
}


@pytest.mark.postgres
class TestChecklistOnEveryPage:
    """Every page that sets a password shows the checklist, wired to its own place."""

    def _assert_checklist(self, page: Any, place: str, token: str | None = None) -> None:
        assert "data-password-feedback" in page.text, place
        assert f'data-place="{place}"' in page.text
        base = PLACES[place]
        assert f'data-policy-url="{base}/password-policy"' in page.text
        assert f'data-check-url="{base}/password-check"' in page.text
        assert 'src="/auth/password-rules.js"' in page.text
        assert 'src="/static/js/password_feedback.js"' in page.text
        assert 'id="password-feedback-list"' in page.text
        if token is not None:
            assert f'data-token="{token}"' in page.text

    def test_the_account_page(self, admin: TestClient) -> None:
        self._assert_checklist(_page(admin, "/account"), "account")

    def test_the_invitation_page(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADMIN, ADMINISTRATOR)
        _, link = invite(plain_client, ADA, USER)
        page = another_client(plain_client).get(link)
        self._assert_checklist(page, "invitation", field(page, "token"))

    def test_the_reset_page(self, outbox: list[Any], plain_client: TestClient) -> None:
        make_user(plain_client, ADA)
        other = another_client(plain_client)
        form = other.get("/auth/password-reset")
        post_form(other, "/auth/password-reset", form, email=ADA)
        page = other.get(link_from(outbox, "/auth/reset"))
        self._assert_checklist(page, "reset", field(page, "token"))

    def test_the_forced_change_page(self, plain_client: TestClient) -> None:
        user = make_user(plain_client, ADA)

        async def require() -> None:
            await _identity(plain_client).password_states.require_change(
                user, ChangeReason.ADMINISTRATOR
            )

        _call(plain_client, require)
        resp = sign_in(plain_client, ADA)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/challenge")
        self._assert_checklist(plain_client.get("/auth/challenge"), "challenge")

    def test_the_set_up_link_is_the_reset_page(
        self, outbox: list[Any], plain_client: TestClient
    ) -> None:
        # An administrator's set-up link for a password is warden's reset link (the same page).
        make_user(plain_client, ADA)
        other = another_client(plain_client)
        post_form(other, "/auth/password-reset", other.get("/auth/password-reset"), email=ADA)
        assert 'data-place="reset"' in other.get(link_from(outbox, "/auth/reset")).text


@pytest.mark.postgres
class TestFeedbackRoutes:
    def _rules(self, answer: Any) -> dict[str, Any]:
        assert answer.status_code == 200, answer.text
        assert answer.headers["cache-control"] == "no-store"
        return answer.json()["rules"]  # type: ignore[no-any-return]

    def test_an_invitation_gets_its_policy_and_a_check(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADMIN, ADMINISTRATOR)
        _, link = invite(plain_client, ADA, USER)
        other = another_client(plain_client)
        page = other.get(link)
        token = field(page, "token")
        base = PLACES["invitation"]
        rules = self._rules(_json(other, base + "/password-policy", page, token=token))
        assert rules["version"] == 1 and rules["min_length"] >= 8
        ok = _json(other, base + "/password-check", page, token=token, password=STRONG)
        assert ok.status_code == 200 and ok.json()["ok"] is True
        weak = _json(other, base + "/password-check", page, token=token, password="short")
        assert weak.json()["ok"] is False
        assert any(r["code"] == "min_length" and not r["passed"] for r in weak.json()["results"])
        assert _json(other, base + "/password-check", page, token=token).status_code == 400

    def test_a_reset_gets_its_policy(self, outbox: list[Any], plain_client: TestClient) -> None:
        make_user(plain_client, ADA)
        other = another_client(plain_client)
        post_form(other, "/auth/password-reset", other.get("/auth/password-reset"), email=ADA)
        page = other.get(link_from(outbox, "/auth/reset"))
        token = field(page, "token")
        rules = self._rules(_json(other, "/auth/reset/password-policy", page, token=token))
        assert rules["min_length"] >= 8
        check = _json(other, "/auth/reset/password-check", page, token=token, password=STRONG)
        assert check.json()["ok"] is True

    def test_a_bad_token_reveals_nothing(self, outbox: list[Any], plain_client: TestClient) -> None:
        make_user(plain_client, ADMIN, ADMINISTRATOR)
        make_user(plain_client, GRACE)
        _, link = invite(plain_client, ADA, USER)
        other = another_client(plain_client)
        invitation = other.get(link)
        post_form(other, "/auth/password-reset", other.get("/auth/password-reset"), email=GRACE)
        reset = other.get(link_from(outbox, "/auth/reset"))
        refused = {"detail": "Password feedback is not available here."}
        for path, page in (("/auth/invitation", invitation), ("/auth/reset", reset)):
            for route in ("/password-policy", "/password-check"):
                for body in ({"token": "not-a-token", "password": "x"}, {}, {"token": 5}):
                    resp = _json(other, path + route, page, **body)
                    assert resp.status_code == 403 and resp.json() == refused, (path, route, body)
        # An invitation's token does not open a reset's route.
        mixed = _json(other, "/auth/reset/password-policy", reset, token=field(invitation, "token"))
        assert mixed.status_code == 403 and mixed.json() == refused

    def test_a_form_route_needs_the_pages_own_token(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADMIN, ADMINISTRATOR)
        _, link = invite(plain_client, ADA, USER)
        other = another_client(plain_client)
        page = other.get(link)
        token = field(page, "token")
        path = "/auth/invitation/password-policy"
        assert other.post(path, json={"token": token}, headers=ORIGIN).status_code == 403
        wrong = other.post(path, json={"token": token}, headers={**ORIGIN, CSRF_HEADER: "nope"})
        assert wrong.status_code == 403
        assert _json(other, path, page, token=token).status_code == 200

    def test_the_forced_change_place_uses_its_challenge(self, plain_client: TestClient) -> None:
        user = make_user(plain_client, ADA)

        async def require() -> None:
            await _identity(plain_client).password_states.require_change(
                user, ChangeReason.ADMINISTRATOR
            )

        _call(plain_client, require)
        # No challenge: refused, without a session or a cookie to use.
        assert (
            plain_client.post(
                "/auth/challenge/password-policy", headers=ORIGIN, json={}
            ).status_code
            == 403
        )
        sign_in(plain_client, ADA)
        page = plain_client.get("/auth/challenge")
        rules = self._rules(_json(plain_client, "/auth/challenge/password-policy", page))
        assert rules["min_length"] >= 8
        check = _json(plain_client, "/auth/challenge/password-check", page, password=STRONG)
        assert check.json()["ok"] is True

    def test_a_second_factor_challenge_is_not_a_change(self, mfa_client: TestClient) -> None:
        from tests.identity_web_helpers import password_step

        make_user(mfa_client, ADA)
        assert password_step(mfa_client, ADA).headers["location"].startswith("/auth/challenge")
        page = mfa_client.get("/auth/challenge")
        resp = _json(mfa_client, "/auth/challenge/password-policy", page)
        assert resp.status_code == 403

    def test_the_account_place_needs_a_session(self, admin: TestClient) -> None:
        rules = self._rules(admin.post("/account/password-policy", json={}))
        assert rules["min_length"] >= 8
        check = admin.post("/account/password-check", json={"password": STRONG})
        assert check.json()["ok"] is True
        signed_out = another_client(admin).post("/account/password-policy", headers=ORIGIN, json={})
        assert signed_out.status_code in (401, 403)
        del admin.headers[CSRF_HEADER]
        assert admin.post("/account/password-policy", json={}).status_code == 403

    def test_the_policy_in_force_is_the_one_described(self, admin: TestClient) -> None:
        policy = _create(admin, "Staff", 1, min_length="20", min_classes="2", max_repeat="2")
        admin.post("/admin/password-policy-roles/administrator", data={"policy_id": str(policy)})
        rules = self._rules(admin.post("/account/password-policy", json={}))
        assert (rules["min_length"], rules["min_classes"], rules["max_repeat"]) == (20, 2, 2)
        weak = admin.post("/account/password-check", json={"password": "aaaaaaaaaaaaaaaaaaaaaaaa"})
        codes = {r["code"]: r["passed"] for r in weak.json()["results"]}
        assert codes["max_repeat"] is False and codes["min_classes"] is False

    def test_a_watch_list_match_shows_its_reason(self, admin: TestClient) -> None:
        listed = _make_list(admin, "Company")
        admin.post(
            f"{LISTS}/{listed}/entries", data={"entries": "synthetic-banned-password-dev-only"}
        )
        default = next(p for p in _policies(admin) if p.is_default)
        form = _policy_form(
            default.settings.name, None, version=str(default.version), watch_lists=[str(listed)]
        )
        assert admin.post(f"{POLICIES}/{default.id}", data=form).status_code in (200, 303)
        answer = admin.post(
            "/account/password-check", json={"password": "synthetic-banned-password-dev-only"}
        )
        body = answer.json()
        assert body["ok"] is False and body["watch_list"] == {"reason": "Company is not allowed"}
        assert admin.post("/account/password-check", json={"password": STRONG}).json()["ok"] is True

    def test_the_server_stays_the_authority_on_submit(self, admin: TestClient) -> None:
        page = _page(admin, "/account")
        resp = admin.post(
            "/account/password",
            data={
                "current": PASSWORD,
                "password": "short",
                "confirm": "short",
                "csrf_token": form_token(page),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400 and message_of(resp)
