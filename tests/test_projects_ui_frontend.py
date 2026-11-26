"""projects.js under Node, and the screens' static guarantees (PC16, PC9 to PC14).

* The sidebar tree: five levels, "Shared with me" with the sharing icon, conversations
  under their project, names as text only, expand and collapse remembered in the browser
  (storage that throws is survived), keyboard navigation.
* What each role is offered on the project page (and nothing the API would refuse).
* The move warning (widen, narrow, shares ending), the delete counts and choices, the
  refusal message of the API let through, upload and quota messages.
* Breadcrumbs: every level a link, the current page not.
* The picker for a conversation in a project offers only the project's people.
* Static: no CDN, only SPARK's own API, no inline markup from payloads.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_preview_frontend import CDN_HOSTS

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
JS = WEB / "static" / "js"
PROJECTS_JS = JS / "projects.js"
TEMPLATES = WEB / "templates"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not installed")

_SCRIPT = r"""
const p = require(process.argv[1]);
const node = (id, name, extra) => Object.assign({id, name, children: [], conversations: [], role: 'owner'}, extra || {});
const deep = node(1, 'L1', {children: [node(2, 'L2', {children: [node(3, 'L3', {children: [node(4, 'L4',
  {children: [node(5, 'L5', {conversations: [{id: 9, name: '<b>Deep</b>', is_mine: true}]})]})]})]})]});
const shared = node(20, 'Theirs', {role: 'viewer', owner_name: 'Ada', conversations: [{id: 21, name: 'Their talk', is_mine: false}]});
const data = {owned: [deep], shared: [shared]};
const mem = () => { const m = {}; return {getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }}; };
const store = mem();
p.saveOpen(store, new Set([1, 2]));
const boom = {getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }};
const html = p.treeHtml(data, new Set([1, 2, 3, 4, 5, 20]), {});
const visible = p.visibleItems(data, new Set([1]));
const view = (o) => Object.assign({id: 1, name: 'Plan', role: 'owner', can_manage: true, can_contribute: true, parent_id: null, depth: 1}, o);
const upload = (status, body) => p.uploadMessage(status, body);
const out = {
  loaded: Array.from(p.loadOpen(store)),
  loadedBad: Array.from(p.loadOpen(boom)),
  savedBad: (() => { try { p.saveOpen(boom, new Set([1])); return true; } catch (e) { return false; } })(),
  loadedJunk: Array.from(p.loadOpen({getItem: () => '{not json'})),
  html,
  collapsedHtml: p.treeHtml(data, new Set(), {}),
  visibleIds: visible.map(v => v.kind + ':' + v.id),
  keys: {
    down: p.navigate(visible, 0, 'ArrowDown'),
    up: p.navigate(visible, 1, 'ArrowUp'),
    upAtTop: p.navigate(visible, 0, 'ArrowUp'),
    right: p.navigate(p.visibleItems(data, new Set()), 0, 'ArrowRight'),
    rightOpen: p.navigate(p.visibleItems(data, new Set([1])), 0, 'ArrowRight'),
    left: p.navigate(visible, 0, 'ArrowLeft'),
    leftChild: p.navigate(visible, 1, 'ArrowLeft'),
    home: p.navigate(visible, 2, 'Home'),
    end: p.navigate(visible, 0, 'End'),
    enter: p.navigate(visible, 0, 'Enter'),
    other: p.navigate(visible, 0, 'x'),
  },
  sections: {
    owner: p.sections(view({}), 'me', true),
    ownerPersonal: p.sections(view({}), 'me', false),
    contributor: p.sections(view({role: 'contributor', can_manage: false}), 'me', true),
    viewer: p.sections(view({role: 'viewer', can_manage: false, can_contribute: false}), 'me', true),
    deepOwner: p.sections(view({depth: 5}), 'me', true),
    sub: p.sections(view({parent_id: 1, depth: 2}), 'me', true),
  },
  removable: [
    p.canRemoveResource(view({}), {added_by: 'x'}, 'me'),
    p.canRemoveResource(view({role: 'contributor', can_manage: false}), {added_by: 'me'}, 'me'),
    p.canRemoveResource(view({role: 'contributor', can_manage: false}), {added_by: 'x'}, 'me'),
    p.canRemoveResource(view({role: 'viewer', can_manage: false, can_contribute: false}), {added_by: 'me'}, 'me'),
  ],
  warnings: [
    p.moveWarning({widened: {g1: 'viewer'}, narrowed: {}, shares_removed: [], names: {g1: 'Cora'}}),
    p.moveWarning({widened: {}, narrowed: {g2: null, g3: 'viewer'}, shares_removed: ['g2', 'g4'], names: {g2: 'Vic', g3: 'Pat'}}),
    p.moveWarning({widened: {}, narrowed: {}, shares_removed: []}),
  ],
  del: {
    text: p.deleteSummary({name: 'Plan', sub_projects: 2, conversations: 5, conversations_by_others: 3, resources: 4, resource_bytes: 2048, members: 2, people_told: 1}),
    one: p.deleteSummary({name: 'Plan', sub_projects: 1, conversations: 1, conversations_by_others: 0, resources: 1, resource_bytes: 1, members: 0, people_told: 0}),
    modes: p.deleteModes({name: 'Plan', parent_id: null}).map(m => m.id),
    keepSub: p.deleteModes({name: 'Plan', parent_id: 3}).find(m => m.id === 'keep').label,
    keepTop: p.deleteModes({name: 'Plan', parent_id: null}).find(m => m.id === 'keep').label,
  },
  upload: [upload(413, {error: 'Too big'}), upload(507, {error: 'The project owner\'s storage is full.'}), upload(403, {error: 'Your role in this project does not allow this.'}), upload(500, null), upload(0, null)],
  refusal: [p.refusal(409, {error: 'Confirm it.'}), p.refusal(403, null), p.refusal(404, {})],
  crumbs: p.breadcrumbsHtml([{id: 1, name: 'Top'}, {id: 2, name: '<i>Sub</i>'}], {current: 'Chat <1>'}),
  crumbsPage: p.breadcrumbsHtml([{id: 1, name: 'Top'}, {id: 2, name: 'Sub'}], {currentIsLast: true}),
  crumbsNone: p.breadcrumbsHtml([], {current: 'Chat'}),
  members: {
    role: [p.roleLabel('viewer'), p.roleLabel('contributor'), p.roleLabel('owner')],
    scope: [p.scopeLabel('project_only'), p.scopeLabel('all_conversations')],
    body: p.memberBody('u-1', 'viewer', 'project_only'),
    bodyNew: p.memberBody(null, 'contributor', 'all_conversations', 'abc'),
  },
  picker: [
    p.pickerPeople([{id: 'w1', name: 'A', email: 'a@example.com'}, {id: 'w2', name: 'B', email: 'b@example.com'}], {people: [{name: 'a'}]}).map(x => x.id),
    p.pickerPeople([{id: 'w1', name: 'A', email: 'a@example.com'}], null).map(x => x.id),
    p.pickerPeople([{id: 'w1', name: 'A', email: 'a@example.com'}], {people: null}).map(x => x.id),
  ],
  size: [p.size(0), p.size(1536), p.size(5 * 1024 * 1024)],
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("Node is not installed")
    done = subprocess.run(
        [NODE, "-e", _SCRIPT, str(PROJECTS_JS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@needs_node
class TestTree:
    def test_expansion_is_remembered_and_survives_bad_storage(self, node: dict[str, Any]) -> None:
        assert sorted(node["loaded"]) == [1, 2]
        assert node["loadedBad"] == []
        assert node["savedBad"] is True  # swallowed, never thrown
        assert node["loadedJunk"] == []

    def test_five_levels_render_nested_with_levels(self, node: dict[str, Any]) -> None:
        html = node["html"]
        for level in range(1, 6):
            assert f'aria-level="{level}"' in html
        assert 'role="tree"' in html and 'role="treeitem"' in html
        assert "L5" in html

    def test_collapsed_projects_hide_their_children(self, node: dict[str, Any]) -> None:
        html = node["collapsedHtml"]
        assert "L1" in html and "L2" not in html
        assert 'aria-expanded="false"' in html

    def test_shared_with_me_carries_the_sharing_icon_and_owner(self, node: dict[str, Any]) -> None:
        html = node["html"]
        assert "Shared with me" in html
        assert "bi-people-fill" in html
        assert "Theirs" in html and "Ada" in html

    def test_conversations_sit_under_their_project_as_links(self, node: dict[str, Any]) -> None:
        assert 'href="/chat/9"' in node["html"]
        assert 'href="/chat/21"' in node["html"]

    def test_names_are_text_never_markup(self, node: dict[str, Any]) -> None:
        assert "<b>Deep</b>" not in node["html"]
        assert "&lt;b&gt;Deep&lt;/b&gt;" in node["html"]

    def test_the_visible_order_follows_the_open_projects(self, node: dict[str, Any]) -> None:
        assert node["visibleIds"] == ["project:1", "project:2", "project:20"]


@needs_node
class TestKeyboard:
    def test_arrows_move_between_visible_items(self, node: dict[str, Any]) -> None:
        keys = node["keys"]
        assert keys["down"] == {"action": "focus", "index": 1}
        assert keys["up"] == {"action": "focus", "index": 0}
        assert keys["upAtTop"] == {"action": "focus", "index": 0}
        assert keys["home"] == {"action": "focus", "index": 0}
        assert keys["end"] == {"action": "focus", "index": 2}

    def test_right_expands_then_enters_and_left_collapses_then_goes_up(
        self, node: dict[str, Any]
    ) -> None:
        keys = node["keys"]
        assert keys["right"] == {"action": "expand", "index": 0}
        assert keys["rightOpen"] == {"action": "focus", "index": 1}
        assert keys["left"] == {"action": "collapse", "index": 0}
        assert keys["leftChild"] == {"action": "focus", "index": 0}

    def test_enter_opens_and_other_keys_do_nothing(self, node: dict[str, Any]) -> None:
        assert node["keys"]["enter"] == {"action": "open", "index": 0}
        assert node["keys"]["other"] is None


@needs_node
class TestSectionsPerRole:
    def test_the_owner_is_offered_everything(self, node: dict[str, Any]) -> None:
        s = node["sections"]["owner"]
        assert all(
            s[k]
            for k in (
                "editInstructions",
                "uploadResource",
                "newSubProject",
                "newConversation",
                "members",
                "transfer",
                "delete",
            )
        )
        assert s["move"] is False  # a top-level project has nowhere to move to

    def test_personal_mode_has_no_members_or_transfer(self, node: dict[str, Any]) -> None:
        s = node["sections"]["ownerPersonal"]
        assert s["members"] is False and s["transfer"] is False
        assert s["delete"] is True and s["editInstructions"] is True

    def test_a_contributor_starts_conversations_and_adds_resources_only(
        self, node: dict[str, Any]
    ) -> None:
        s = node["sections"]["contributor"]
        assert s["newConversation"] and s["uploadResource"]
        assert not any(
            s[k] for k in ("editInstructions", "newSubProject", "members", "transfer", "delete")
        )

    def test_read_only_changes_nothing(self, node: dict[str, Any]) -> None:
        s = node["sections"]["viewer"]
        assert not any(
            s[k]
            for k in (
                "editInstructions",
                "uploadResource",
                "newSubProject",
                "newConversation",
                "members",
                "transfer",
                "delete",
                "move",
            )
        )

    def test_no_sub_project_beyond_level_five_and_a_sub_project_can_move(
        self, node: dict[str, Any]
    ) -> None:
        assert node["sections"]["deepOwner"]["newSubProject"] is False
        sub = node["sections"]["sub"]
        assert sub["move"] is True and sub["transfer"] is False and sub["members"] is False

    def test_resource_removal_follows_the_role(self, node: dict[str, Any]) -> None:
        assert node["removable"] == [True, True, False, False]


@needs_node
class TestDialogs:
    def test_the_move_warning_names_who_is_affected(self, node: dict[str, Any]) -> None:
        widen, narrow, none = node["warnings"]
        assert widen["level"] == "widen" and "Cora would see it as Read-only." in widen["lines"]
        assert narrow["level"] == "narrow"
        assert "Vic would no longer see it." in narrow["lines"]
        assert "Pat would keep it only as Read-only." in narrow["lines"]
        assert "g4 would lose the share you gave them." in narrow["lines"] or any(
            "lose the share" in line for line in narrow["lines"]
        )
        assert none == {"level": "none", "lines": []}

    def test_the_delete_dialog_names_the_counts(self, node: dict[str, Any]) -> None:
        text = node["del"]["text"]
        assert "5 conversations" in text and "3 started by others" in text
        assert "4 resources" in text and "2 sub-projects" in text and "2 members" in text
        assert "1 person" in text
        one = node["del"]["one"]
        assert "1 conversation" in one and "1 resource" in one and "1 sub-project" in one
        assert "0 members" not in one

    def test_delete_offers_keep_or_everything_worded_for_the_level(
        self, node: dict[str, Any]
    ) -> None:
        d = node["del"]
        assert d["modes"] == ["keep", "everything"]
        assert "parent" in d["keepSub"].lower()
        assert "no project" in d["keepTop"].lower()

    def test_the_apis_refusal_message_is_let_through(self, node: dict[str, Any]) -> None:
        assert node["refusal"][0] == "Confirm it."
        assert node["refusal"][1] and "403" not in node["refusal"][1]
        assert node["upload"][0] == "Too big"
        assert node["upload"][1] == "The project owner's storage is full."
        assert node["upload"][2] == "Your role in this project does not allow this."
        assert "failed" in node["upload"][3].lower() or "could not" in node["upload"][3].lower()
        assert "network" in node["upload"][4].lower() or "reach" in node["upload"][4].lower()

    def test_member_labels_and_bodies(self, node: dict[str, Any]) -> None:
        m = node["members"]
        assert m["role"] == ["Read-only", "Contributor", "Owner"]
        assert m["scope"] == ["Project only", "Project and all conversations"]
        assert m["body"] == {"user_guid": "u-1", "role": "viewer", "scope": "project_only"}
        assert m["bodyNew"] == {
            "user_id": "abc",
            "role": "contributor",
            "scope": "all_conversations",
        }

    def test_sizes_read_as_text(self, node: dict[str, Any]) -> None:
        assert node["size"] == ["0 B", "1.5 KB", "5.0 MB"]


@needs_node
class TestBreadcrumbs:
    def test_every_level_is_a_link_and_the_conversation_is_current(
        self, node: dict[str, Any]
    ) -> None:
        html = node["crumbs"]
        assert 'href="/projects/1"' in html and 'href="/projects/2"' in html
        assert 'aria-label="Project path"' in html
        assert 'aria-current="page"' in html
        assert "<i>Sub</i>" not in html and "&lt;i&gt;Sub&lt;/i&gt;" in html
        assert "Chat &lt;1&gt;" in html

    def test_a_project_page_ends_on_its_own_unlinked_level(self, node: dict[str, Any]) -> None:
        html = node["crumbsPage"]
        assert 'href="/projects/1"' in html and 'href="/projects/2"' not in html
        assert html.count('aria-current="page"') == 1

    def test_no_path_no_breadcrumbs(self, node: dict[str, Any]) -> None:
        assert node["crumbsNone"] == ""


@needs_node
class TestSharePicker:
    def test_under_a_project_only_its_people_are_offered(self, node: dict[str, Any]) -> None:
        assert node["picker"][0] == ["w1"]
        assert node["picker"][1] == ["w1"]  # not in a project: everyone
        assert node["picker"][2] == ["w1"]  # people not known to this user: the API decides


class TestStatic:
    def test_no_cdn_and_only_sparks_own_api(self) -> None:
        text = PROJECTS_JS.read_text(encoding="utf-8")
        assert not CDN_HOSTS.search(text)
        for url in re.findall(r"""fetch\(\s*[`'"]([^`'"$]+)""", text):
            assert url.startswith(("/projects/", "/conversations/")), url

    def test_payload_values_are_never_assigned_as_markup_unescaped(self) -> None:
        text = PROJECTS_JS.read_text(encoding="utf-8")
        assert "eval(" not in text and "document.write" not in text
        assert "esc(" in text

    def test_the_templates_exist_and_load_the_script_from_spark(self) -> None:
        for name in (
            "project.html",
            "_project_members_dialog.html",
            "_project_move_dialog.html",
            "_project_delete_dialog.html",
        ):
            assert (TEMPLATES / name).is_file(), name
        page = (TEMPLATES / "project.html").read_text(encoding="utf-8")
        assert "/static/js/projects.js" in page and "/static/css/projects.css" in page
        assert not CDN_HOSTS.search(page)
