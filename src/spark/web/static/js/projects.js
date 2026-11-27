/**
 * Spark: projects in the interface (PC16, with the screens for PC9 to PC14).
 *
 * The logic part (no DOM) is exported under Node for tests; the page part drives
 *   - the Projects tree on the Conversations page (five levels, "Shared with me",
 *     expansion remembered per browser, keyboard navigation);
 *   - the project page (description, instructions with a Markdown preview, resources,
 *     sub-projects, conversations, members for the owner) and its dialogs (move,
 *     delete, members, transfer);
 *   - the breadcrumbs and the Move button in the chat header;
 *   - the people a conversation in a project may be shared with.
 *
 * Every screen offers only what the API permits for the user's role and scope, and shows
 * the API's own refusal message when it refuses. Every value from a payload is escaped or
 * set as text; the project routes are the only ones fetched (and the conversation routes
 * for people and creation).
 */
(function (root, factory) {
    'use strict';
    const logic = factory();
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = logic;
    } else {
        root.SparkProjects = logic;
        if (root.document) logic.boot(root);
    }
}(typeof window !== 'undefined' ? window : this, function () {
    'use strict';

    const MAX_DEPTH = 5;
    const STORE_KEY = 'spark.projects.open';
    const ROLE_LABELS = { viewer: 'Read-only', contributor: 'Contributor', owner: 'Owner' };
    const SCOPE_LABELS = { project_only: 'Project only', all_conversations: 'Project and all conversations' };
    const SHARE_ICON = '<i class="bi bi-people-fill project-share-icon" role="img" aria-label="Shared with you" title="Shared with you"></i>';

    // -- Small helpers ---------------------------------------------------------------

    function esc(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function plural(n, one, many) {
        return `${n} ${n === 1 ? one : (many || one + 's')}`;
    }

    function size(bytes) {
        const n = Number(bytes) || 0;
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
        return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    }

    function roleLabel(role) { return ROLE_LABELS[role] || String(role || ''); }
    function scopeLabel(scope) { return SCOPE_LABELS[scope] || String(scope || ''); }

    // -- Expansion, remembered per browser -------------------------------------------

    function loadOpen(storage) {
        try {
            const raw = storage.getItem(STORE_KEY);
            const list = raw ? JSON.parse(raw) : [];
            return new Set(Array.isArray(list) ? list.filter(Number.isInteger) : []);
        } catch (err) {
            return new Set();
        }
    }

    function saveOpen(storage, open) {
        try {
            storage.setItem(STORE_KEY, JSON.stringify(Array.from(open)));
        } catch (err) { /* private mode or storage refused: not remembered */ }
    }

    // -- The tree --------------------------------------------------------------------

    function conversationItem(c, level) {
        return `<li role="treeitem" class="project-tree-item project-tree-conversation" aria-level="${level}" data-kind="conversation" data-id="${esc(c.id)}">` +
            `<a class="project-tree-link" href="/chat/${esc(c.id)}" tabindex="-1">` +
            `<i class="bi bi-chat-dots" aria-hidden="true"></i> <span>${esc(c.name)}</span></a></li>`;
    }

    function projectItem(node, level, open, shared) {
        const kids = node.children || [];
        const talks = node.conversations || [];
        const expandable = kids.length > 0 || talks.length > 0;
        const isOpen = expandable && open.has(Number(node.id));
        let html = `<li role="treeitem" class="project-tree-item" aria-level="${level}" data-kind="project" data-id="${esc(node.id)}"` +
            (expandable ? ` aria-expanded="${isOpen ? 'true' : 'false'}"` : '') + '>' +
            '<div class="project-tree-row">' +
            (expandable
                ? `<button type="button" class="project-tree-toggle" tabindex="-1" data-toggle="${esc(node.id)}" aria-label="${isOpen ? 'Collapse' : 'Expand'} ${esc(node.name)}"><i class="bi ${isOpen ? 'bi-chevron-down' : 'bi-chevron-right'}" aria-hidden="true"></i></button>`
                : '<span class="project-tree-toggle project-tree-toggle-empty" aria-hidden="true"></span>') +
            `<a class="project-tree-link" href="/projects/${esc(node.id)}" tabindex="-1">` +
            `<i class="bi bi-folder2${isOpen ? '-open' : ''}" aria-hidden="true"></i> <span>${esc(node.name)}</span></a>` +
            (shared && level === 1 ? `${SHARE_ICON}<span class="project-tree-owner">${esc(node.owner_name || '')}</span>` : '') +
            '</div>';
        if (isOpen) {
            html += '<ul role="group" class="project-tree-group">' +
                kids.map(k => projectItem(k, level + 1, open, false)).join('') +
                talks.map(c => conversationItem(c, level + 1)).join('') + '</ul>';
        }
        return html + '</li>';
    }

    function treeHtml(data, open, options) {
        const owned = (data && data.owned) || [];
        const shared = (data && data.shared) || [];
        let html = '';
        if (owned.length) {
            html += '<ul role="tree" aria-label="Projects" class="project-tree">' +
                owned.map(n => projectItem(n, 1, open, false)).join('') + '</ul>';
        }
        if (shared.length) {
            html += '<div class="project-tree-shared"><h6 class="project-tree-heading">' +
                `${SHARE_ICON} Shared with me</h6>` +
                '<ul role="tree" aria-label="Shared with me" class="project-tree">' +
                shared.map(n => projectItem(n, 1, open, true)).join('') + '</ul></div>';
        }
        return html;
    }

    /** The items shown now, in order: [{kind, id, level, expandable, expanded, parent}]. */
    function visibleItems(data, open) {
        const out = [];
        function walk(node, level, parent) {
            const kids = node.children || [];
            const talks = node.conversations || [];
            const expandable = kids.length + talks.length > 0;
            const expanded = expandable && open.has(Number(node.id));
            const at = out.length;
            out.push({ kind: 'project', id: node.id, level, expandable, expanded, parent });
            if (expanded) {
                kids.forEach(k => walk(k, level + 1, at));
                talks.forEach(c => out.push({ kind: 'conversation', id: c.id, level: level + 1, expandable: false, expanded: false, parent: at }));
            }
        }
        ((data && data.owned) || []).forEach(n => walk(n, 1, -1));
        ((data && data.shared) || []).forEach(n => walk(n, 1, -1));
        return out;
    }

    /** What a key does at ``index`` among the visible items, or null. */
    function navigate(items, index, key) {
        const last = items.length - 1;
        const here = items[index];
        if (!here) return null;
        switch (key) {
            case 'ArrowDown': return { action: 'focus', index: Math.min(index + 1, last) };
            case 'ArrowUp': return { action: 'focus', index: Math.max(index - 1, 0) };
            case 'Home': return { action: 'focus', index: 0 };
            case 'End': return { action: 'focus', index: last };
            case 'ArrowRight':
                if (here.expandable && !here.expanded) return { action: 'expand', index };
                return index < last && items[index + 1].parent === index ? { action: 'focus', index: index + 1 } : null;
            case 'ArrowLeft':
                if (here.expanded) return { action: 'collapse', index };
                return here.parent >= 0 ? { action: 'focus', index: here.parent } : null;
            case 'Enter':
            case ' ':
                return { action: 'open', index };
            default: return null;
        }
    }

    // -- What each role is offered ----------------------------------------------------

    function sections(project, me, server) {
        const manage = !!project.can_manage;
        const contribute = !!project.can_contribute;
        const top = project.parent_id == null;
        return {
            editInstructions: manage,
            uploadResource: contribute,
            newSubProject: manage && Number(project.depth || 1) < MAX_DEPTH,
            newConversation: contribute,
            members: manage && !!server && top,
            transfer: manage && !!server && top,
            move: manage && !top,
            delete: manage,
        };
    }

    function canRemoveResource(project, resource, me) {
        if (project.can_manage) return true;
        return !!project.can_contribute && resource.added_by === me;
    }

    // -- Messages --------------------------------------------------------------------

    function refusal(status, body) {
        if (body && body.error) return String(body.error);
        if (status === 403) return 'Your role in this project does not allow this.';
        if (status === 404) return 'That was not found, or you no longer have access to it.';
        return 'That did not work. Please try again.';
    }

    function uploadMessage(status, body) {
        if (status === 0) return 'The upload could not reach Spark. Check your network and try again.';
        if (body && body.error) return String(body.error);
        if (status === 413) return 'That file is larger than the upload limit.';
        if (status === 507) return 'The project owner\'s storage is full.';
        return 'The upload failed. Please try again.';
    }

    function moveWarning(report) {
        const widened = (report && report.widened) || {};
        const narrowed = (report && report.narrowed) || {};
        const removed = (report && report.shares_removed) || [];
        const names = (report && report.names) || {};
        const name = g => names[g] || g;
        const lines = [];
        Object.keys(widened).forEach(g => lines.push(`${name(g)} would see it as ${roleLabel(widened[g])}.`));
        Object.keys(narrowed).forEach(g => lines.push(narrowed[g]
            ? `${name(g)} would keep it only as ${roleLabel(narrowed[g])}.`
            : `${name(g)} would no longer see it.`));
        removed.forEach(g => {
            if (!Object.prototype.hasOwnProperty.call(narrowed, g)) lines.push(`${name(g)} would lose the share you gave them.`);
        });
        const narrow = Object.keys(narrowed).length > 0 || removed.length > 0;
        const widen = Object.keys(widened).length > 0;
        return { level: widen && narrow ? 'both' : widen ? 'widen' : narrow ? 'narrow' : 'none', lines };
    }

    function deleteSummary(preview) {
        const p = preview || {};
        const parts = [
            plural(p.conversations || 0, 'conversation') + (p.conversations_by_others ? ` (${p.conversations_by_others} started by others)` : ''),
            plural(p.resources || 0, 'resource') + (p.resource_bytes ? ` (${size(p.resource_bytes)})` : ''),
            plural(p.sub_projects || 0, 'sub-project'),
        ];
        if (p.members) parts.push(plural(p.members, 'member'));
        let text = `Deleting "${p.name || ''}" affects ${parts.join(', ')}.`;
        if (p.people_told) text += ` ${plural(p.people_told, 'person', 'people')} will be told that their conversations were deleted.`;
        return text;
    }

    function deleteModes(project) {
        const sub = project && project.parent_id != null;
        return [
            {
                id: 'keep',
                label: sub
                    ? 'Keep the conversations: they move up to the parent project'
                    : 'Keep the conversations: they move to no project, private to whoever started each',
            },
            { id: 'everything', label: 'Delete everything, including other people\'s conversations' },
        ];
    }

    // -- Breadcrumbs ------------------------------------------------------------------

    function breadcrumbsHtml(path, options) {
        const levels = path || [];
        if (!levels.length) return '';
        const opts = options || {};
        const items = levels.map((p, i) => {
            const lastOfPage = opts.currentIsLast && i === levels.length - 1;
            return lastOfPage
                ? `<li class="breadcrumb-item active" aria-current="page">${esc(p.name)}</li>`
                : `<li class="breadcrumb-item"><a href="/projects/${esc(p.id)}">${esc(p.name)}</a></li>`;
        });
        if (opts.current) items.push(`<li class="breadcrumb-item active" aria-current="page">${esc(opts.current)}</li>`);
        return `<nav aria-label="Project path"><ol class="breadcrumb project-crumbs">${items.join('')}</ol></nav>`;
    }

    // -- Members ----------------------------------------------------------------------

    function memberBody(guid, role, scope, userId) {
        const body = guid ? { user_guid: guid } : { user_id: userId };
        body.role = role;
        body.scope = scope;
        return body;
    }

    /** The people a conversation in a project may be shared with. ``limit`` is null (no
     * project), or {people: [{id}]} (the project's people, by warden user id), or
     * {people: null} (the read failed: the API still refuses anyone outside the project). */
    function pickerPeople(found, limit) {
        if (!limit || !limit.people) return found;
        const ids = new Set(limit.people.map(p => String(p.id || '').toLowerCase()));
        return found.filter(p => ids.has(String(p.id || '').toLowerCase()));
    }

    const logic = {
        MAX_DEPTH, esc, size, roleLabel, scopeLabel, loadOpen, saveOpen, treeHtml, visibleItems,
        navigate, sections, canRemoveResource, refusal, uploadMessage, moveWarning,
        deleteSummary, deleteModes, breadcrumbsHtml, memberBody, pickerPeople,
    };

    // -- The page part -----------------------------------------------------------------

    function boot(win) {
        const doc = win.document;
        const $ = id => doc.getElementById(id);
        const jsonHeaders = { 'Content-Type': 'application/json', Accept: 'application/json' };

        async function call(method, url, body) {
            let resp;
            try {
                resp = await win.fetch(url, {
                    method,
                    headers: body === undefined ? { Accept: 'application/json' } : jsonHeaders,
                    body: body === undefined ? undefined : JSON.stringify(body),
                });
            } catch (err) {
                return { ok: false, status: 0, body: null };
            }
            let data = null;
            try { data = await resp.json(); } catch (err) { /* no body */ }
            return { ok: resp.ok, status: resp.status, body: data };
        }

        function toast(kind, text) {
            if (win.AppToast && win.AppToast[kind]) win.AppToast[kind](kind === 'danger' ? 'Error' : 'Done', text);
        }

        function show(id, text) {
            const el = $(id);
            if (!el) return;
            el.textContent = text || '';
            el.hidden = !text;
        }

        function modal(id) {
            const el = $(id);
            return el && win.bootstrap ? win.bootstrap.Modal.getOrCreateInstance(el) : null;
        }

        function node(tag, cls, text) {
            const n = doc.createElement(tag);
            if (cls) n.className = cls;
            if (text !== undefined) n.textContent = text;
            return n;
        }

        // The Projects tree ----------------------------------------------------------

        function initTree() {
            const host = $('projects-tree');
            if (!host) return;
            const open = loadOpen(win.localStorage || { getItem() { throw new Error('none'); } });
            let data = { owned: [], shared: [] };
            let focused = 0;

            function persist() { saveOpen(win.localStorage || { setItem() { throw new Error('none'); } }, open); }

            function draw(focusIndex) {
                host.innerHTML = treeHtml(data, open, {});
                const empty = $('projects-empty');
                if (empty) empty.hidden = !!(data.owned.length || data.shared.length);
                const rows = host.querySelectorAll('[role="treeitem"]');
                focused = Math.max(0, Math.min(focusIndex == null ? focused : focusIndex, rows.length - 1));
                rows.forEach((row, i) => row.setAttribute('tabindex', i === focused ? '0' : '-1'));
                host.querySelectorAll('[data-toggle]').forEach(btn => btn.addEventListener('click', ev => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    toggle(Number(btn.dataset.toggle));
                }));
            }

            function toggle(id, force) {
                const want = force == null ? !open.has(id) : force;
                if (want) open.add(id); else open.delete(id);
                persist();
                draw();
            }

            host.addEventListener('keydown', ev => {
                const item = ev.target.closest && ev.target.closest('[role="treeitem"]');
                if (!item) return;
                const items = visibleItems(data, open);
                const rows = Array.from(host.querySelectorAll('[role="treeitem"]'));
                const index = rows.indexOf(item);
                const step = navigate(items, index, ev.key);
                if (!step) return;
                ev.preventDefault();
                if (step.action === 'focus') {
                    draw(step.index);
                    host.querySelectorAll('[role="treeitem"]')[step.index].focus();
                } else if (step.action === 'expand' || step.action === 'collapse') {
                    toggle(Number(items[step.index].id), step.action === 'expand');
                    host.querySelectorAll('[role="treeitem"]')[step.index].focus();
                } else {
                    const link = item.querySelector('a.project-tree-link');
                    if (link) win.location.href = link.getAttribute('href');
                }
            });

            call('GET', '/projects/api').then(res => {
                if (!res.ok) { host.textContent = refusal(res.status, res.body); return; }
                data = res.body;
                if (!open.size) data.owned.forEach(n => open.add(Number(n.id)));
                draw(0);
                const pick = $('move-project-options');
                if (pick) pick.dataset.ready = '1';
            });
            win.SparkProjects.reloadTree = () => call('GET', '/projects/api').then(r => { if (r.ok) { data = r.body; draw(); } });
        }

        // New project ----------------------------------------------------------------

        function initNewProject() {
            const form = $('new-project-form');
            if (!form) return;
            form.addEventListener('submit', async ev => {
                ev.preventDefault();
                show('new-project-error', '');
                const parent = form.dataset.parent ? Number(form.dataset.parent) : null;
                const res = await call('POST', '/projects/api', {
                    name: $('new-project-name').value,
                    description: $('new-project-description').value,
                    parent_id: parent,
                });
                if (!res.ok) { show('new-project-error', refusal(res.status, res.body)); return; }
                win.location.href = `/projects/${res.body.project.id}`;
            });
            doc.querySelectorAll('[data-new-project]').forEach(btn => btn.addEventListener('click', () => {
                form.dataset.parent = btn.dataset.newProject || '';
                $('new-project-title').textContent = btn.dataset.newProject ? 'New sub-project' : 'New project';
                $('new-project-name').value = '';
                $('new-project-description').value = '';
                show('new-project-error', '');
                const m = modal('newProjectModal');
                if (m) m.show();
            }));
        }

        // The project page -----------------------------------------------------------

        function initPage() {
            const page = $('project-page');
            if (!page) return;
            const id = Number(page.dataset.projectId);
            const server = page.dataset.server === '1';
            const me = page.dataset.me || '';
            let view = null;

            function actions(tpl) { return tpl; }

            async function load() {
                const res = await call('GET', `/projects/api/${id}`);
                if (!res.ok) {
                    show('project-error', refusal(res.status, res.body));
                    return;
                }
                view = res.body;
                draw();
            }

            function draw() {
                const p = view.project;
                const s = sections(p, me, server);
                $('project-crumbs').innerHTML = breadcrumbsHtml(view.path, { currentIsLast: true });
                $('project-name').textContent = p.name;
                $('project-description').textContent = p.description || 'No description yet.';
                $('project-role').textContent = p.role === 'owner' ? 'Owner' : `${roleLabel(p.role)}${p.scope ? ', ' + scopeLabel(p.scope).toLowerCase() : ''}`;
                $('project-role').hidden = false;
                $('project-edit-details').hidden = !s.editInstructions;
                // Instructions
                const text = p.instructions || '';
                $('instructions-view').innerHTML = text && win.SparkMarkdown ? win.SparkMarkdown.render(text) : '';
                if (!text) $('instructions-view').textContent = 'No instructions yet.';
                $('instructions-edit').hidden = !s.editInstructions;
                $('instructions-editor-wrap').hidden = true;
                $('instructions-text').value = text;
                // Resources
                $('resources-upload').hidden = !s.uploadResource;
                const list = $('resources-list');
                list.textContent = '';
                if (!view.resources.length) list.appendChild(node('li', 'text-muted small', 'No resources yet.'));
                view.resources.forEach(r => {
                    const li = node('li', 'project-resource d-flex align-items-center gap-2');
                    const a = node('a', 'flex-grow-1', r.filename);
                    a.href = `/projects/api/${id}/resources/${r.file_id}`;
                    a.setAttribute('download', r.filename);
                    li.appendChild(a);
                    li.appendChild(node('span', 'text-muted small', size(r.size_bytes)));
                    if (canRemoveResource(p, r, me)) {
                        const b = node('button', 'btn btn-app-ghost btn-sm');
                        b.type = 'button';
                        b.setAttribute('aria-label', `Remove ${r.filename}`);
                        b.innerHTML = '<i class="bi bi-trash" aria-hidden="true"></i>';
                        b.addEventListener('click', () => removeResource(r));
                        li.appendChild(b);
                    }
                    list.appendChild(li);
                });
                // Sub-projects
                $('subprojects-new').hidden = !s.newSubProject;
                $('subprojects-new').dataset.newProject = String(id);
                const subs = $('subprojects-list');
                subs.textContent = '';
                if (!view.sub_projects.length) subs.appendChild(node('li', 'text-muted small', 'No sub-projects.'));
                view.sub_projects.forEach(sp => {
                    const li = node('li');
                    const a = node('a', '', sp.name);
                    a.href = `/projects/${sp.id}`;
                    li.appendChild(a);
                    subs.appendChild(li);
                });
                // Conversations
                $('conversation-new').hidden = !s.newConversation;
                const convs = $('conversations-list');
                convs.textContent = '';
                if (!view.conversations.length) convs.appendChild(node('li', 'text-muted small', 'No conversations yet.'));
                view.conversations.forEach(c => {
                    const li = node('li');
                    const a = node('a', '', c.name);
                    a.href = `/chat/${c.id}`;
                    li.appendChild(a);
                    if (!c.is_mine) li.appendChild(node('span', 'text-muted small ms-2', 'started by someone else'));
                    convs.appendChild(li);
                });
                // Members, move, transfer, delete
                $('members-section').hidden = !s.members;
                if (s.members) drawMembers();
                $('project-move').hidden = !s.move;
                $('project-transfer').hidden = !s.transfer;
                $('project-delete').hidden = !s.delete;
                doc.title = `Spark - ${p.name}`;
            }

            // Instructions editor with live preview
            $('instructions-edit').addEventListener('click', () => {
                $('instructions-editor-wrap').hidden = false;
                $('instructions-edit').hidden = true;
                preview();
                $('instructions-text').focus();
            });
            function preview() {
                const text = $('instructions-text').value;
                $('instructions-preview').innerHTML = text && win.SparkMarkdown ? win.SparkMarkdown.render(text) : '';
            }
            $('instructions-text').addEventListener('input', preview);
            $('instructions-cancel').addEventListener('click', () => {
                $('instructions-text').value = view.project.instructions || '';
                draw();
            });
            $('instructions-save').addEventListener('click', async () => {
                show('instructions-error', '');
                const res = await call('PATCH', `/projects/api/${id}`, { instructions: $('instructions-text').value });
                if (!res.ok) { show('instructions-error', refusal(res.status, res.body)); return; }
                toast('success', 'Instructions saved.');
                await load();
            });

            // Name and description
            $('project-edit-details').addEventListener('click', async () => {
                const name = win.prompt('Project name', view.project.name);
                if (name == null) return;
                const description = win.prompt('Description', view.project.description || '');
                if (description == null) return;
                const res = await call('PATCH', `/projects/api/${id}`, { name, description });
                if (!res.ok) { toast('danger', refusal(res.status, res.body)); return; }
                await load();
            });

            // Resources
            $('resource-file').addEventListener('change', async ev => {
                const file = ev.target.files[0];
                ev.target.value = '';
                if (!file) return;
                show('resources-error', '');
                const form = new win.FormData();
                form.append('file', file);
                let resp;
                try {
                    resp = await win.fetch(`/projects/api/${id}/resources`, { method: 'POST', body: form });
                } catch (err) {
                    show('resources-error', uploadMessage(0, null));
                    return;
                }
                let body = null;
                try { body = await resp.json(); } catch (err) { /* none */ }
                if (!resp.ok) { show('resources-error', uploadMessage(resp.status, body)); return; }
                await load();
            });
            async function removeResource(r) {
                if (!win.confirm(`Remove "${r.filename}" from this project?`)) return;
                const res = await call('DELETE', `/projects/api/${id}/resources/${r.file_id}`);
                if (!res.ok) { show('resources-error', refusal(res.status, res.body)); return; }
                await load();
            }

            // New conversation at this level
            $('conversation-new').addEventListener('click', async () => {
                const res = await call('GET', '/conversations/api/models');
                const models = res.ok && Array.isArray(res.body) ? res.body : [];
                const choice = models.find(m => m.default) || models[0];
                const made = await call('POST', '/conversations/api/create', {
                    name: 'New conversation',
                    model_id: choice ? (choice.id || choice.model_id) : '',
                    project_id: id,
                });
                if (!made.ok) { show('conversations-error', refusal(made.status, made.body)); return; }
                win.location.href = `/chat/${made.body.id || made.body.conversation_id || (made.body.conversation && made.body.conversation.id)}`;
            });

            // Members (owner only)
            function drawMembers() {
                const host = $('members-list');
                host.textContent = '';
                const rows = view.members || [];
                if (!rows.length) host.appendChild(node('div', 'text-muted small', 'Not shared with anyone yet.'));
                rows.forEach(m => {
                    const row = node('div', 'project-member d-flex align-items-center gap-2 flex-wrap mb-2');
                    row.appendChild(node('span', 'participant-initials', m.initials || '?'));
                    row.appendChild(node('span', 'flex-grow-1', m.name));
                    const role = selectOf(['viewer', 'contributor'], m.role, roleLabel);
                    const scope = selectOf(['project_only', 'all_conversations'], m.scope, scopeLabel);
                    role.setAttribute('aria-label', `Role of ${m.name}`);
                    scope.setAttribute('aria-label', `Scope for ${m.name}`);
                    const change = async () => {
                        const res = await call('POST', `/projects/api/${id}/members`, memberBody(m.user_guid, role.value, scope.value));
                        if (!res.ok) { show('members-error', refusal(res.status, res.body)); return; }
                        await load();
                    };
                    role.addEventListener('change', change);
                    scope.addEventListener('change', change);
                    row.appendChild(role);
                    row.appendChild(scope);
                    const remove = node('button', 'btn btn-app-ghost btn-sm');
                    remove.type = 'button';
                    remove.setAttribute('aria-label', `Remove ${m.name}`);
                    remove.innerHTML = '<i class="bi bi-person-dash" aria-hidden="true"></i>';
                    remove.addEventListener('click', async () => {
                        const res = await call('DELETE', `/projects/api/${id}/members/${encodeURIComponent(m.user_guid)}`);
                        if (!res.ok) { show('members-error', refusal(res.status, res.body)); return; }
                        await load();
                    });
                    row.appendChild(remove);
                    const makeOwner = node('button', 'btn btn-app-ghost btn-sm', 'Make owner');
                    makeOwner.type = 'button';
                    makeOwner.addEventListener('click', () => transfer({ user_guid: m.user_guid }, m.name));
                    row.appendChild(makeOwner);
                    host.appendChild(row);
                });
            }
            function selectOf(values, current, label) {
                const sel = node('select', 'form-select form-select-sm project-member-select');
                values.forEach(v => {
                    const o = node('option', '', label(v));
                    o.value = v;
                    if (v === current) o.selected = true;
                    sel.appendChild(o);
                });
                return sel;
            }

            // People picker for new members and transfer
            let timer = null;
            function pickPeople(inputId, resultsId, onPick) {
                const input = $(inputId);
                input.addEventListener('input', () => {
                    clearTimeout(timer);
                    const q = input.value.trim();
                    const box = $(resultsId);
                    if (q.length < 2) { box.textContent = ''; return; }
                    timer = setTimeout(async () => {
                        const res = await call('GET', `/conversations/api/people?q=${encodeURIComponent(q)}`);
                        box.textContent = '';
                        const people = res.ok && Array.isArray(res.body) ? res.body : [];
                        if (!people.length) { box.appendChild(node('div', 'form-text', 'No one found.')); return; }
                        people.forEach(p => {
                            const b = node('button', 'btn btn-app-ghost btn-sm d-block text-start');
                            b.type = 'button';
                            b.textContent = `${p.name} (${p.email})`;
                            b.addEventListener('click', () => onPick(p));
                            box.appendChild(b);
                        });
                    }, 250);
                });
            }
            pickPeople('member-search', 'member-results', async person => {
                show('members-error', '');
                const res = await call('POST', `/projects/api/${id}/members`,
                    memberBody(null, $('member-role').value, $('member-scope').value, person.id));
                if (!res.ok) { show('members-error', refusal(res.status, res.body)); return; }
                $('member-search').value = '';
                $('member-results').textContent = '';
                await load();
            });

            // Transfer
            async function transfer(who, label) {
                if (!win.confirm(`Make ${label} the owner of "${view.project.name}"? You stay as a Contributor with all conversations. Their storage must hold the project's resources.`)) return;
                const res = await call('POST', `/projects/api/${id}/transfer`, who);
                if (!res.ok) { show('members-error', refusal(res.status, res.body)); return; }
                win.location.reload();
            }
            $('project-transfer').addEventListener('click', () => {
                show('transfer-error', '');
                const m = modal('transferModal');
                if (m) m.show();
            });
            pickPeople('transfer-search', 'transfer-results', async person => {
                if (!win.confirm(`Make ${person.name} the owner of "${view.project.name}"? They are added as a member in the same step.`)) return;
                const res = await call('POST', `/projects/api/${id}/transfer`, { user_id: person.id });
                if (!res.ok) { show('transfer-error', refusal(res.status, res.body)); return; }
                win.location.reload();
            });

            // Delete
            $('project-delete').addEventListener('click', async () => {
                show('delete-error', '');
                const res = await call('GET', `/projects/api/${id}/delete-preview`);
                if (!res.ok) { toast('danger', refusal(res.status, res.body)); return; }
                $('delete-summary').textContent = deleteSummary(res.body);
                const box = $('delete-modes');
                box.textContent = '';
                deleteModes(view.project).forEach((m, i) => {
                    const wrap = node('div', 'form-check');
                    const input = node('input', 'form-check-input');
                    input.type = 'radio';
                    input.name = 'delete-mode';
                    input.id = `delete-mode-${m.id}`;
                    input.value = m.id;
                    input.checked = i === 0;
                    const label = node('label', 'form-check-label', m.label);
                    label.htmlFor = input.id;
                    wrap.appendChild(input);
                    wrap.appendChild(label);
                    box.appendChild(wrap);
                });
                const m = modal('deleteProjectModal');
                if (m) m.show();
            });
            $('delete-confirm').addEventListener('click', async () => {
                const chosen = doc.querySelector('input[name="delete-mode"]:checked');
                const res = await call('POST', `/projects/api/${id}/delete`, { mode: chosen ? chosen.value : '' });
                if (!res.ok) { show('delete-error', refusal(res.status, res.body)); return; }
                win.location.href = view.project.parent_id ? `/projects/${view.project.parent_id}` : '/conversations';
            });

            // Move (sub-projects)
            $('project-move').addEventListener('click', async () => {
                show('move-error', '');
                const res = await call('GET', '/projects/api');
                const sel = $('move-destination');
                sel.textContent = '';
                const flat = [];
                (function walk(nodes) { nodes.forEach(n => { flat.push(n); walk(n.children || []); }); }(
                    (res.ok ? res.body.owned : []).filter(n => Number(n.id) === Number(view.project.root_id))));
                flat.filter(n => Number(n.id) !== id).forEach(n => {
                    const o = node('option', '', `${'  '.repeat(n.depth - 1)}${n.name}`);
                    o.value = n.id;
                    sel.appendChild(o);
                });
                $('move-warning').hidden = true;
                const m = modal('moveModal');
                if (m) m.show();
            });
            $('move-confirm').addEventListener('click', async () => {
                const res = await call('POST', `/projects/api/${id}/move`, { parent_id: Number($('move-destination').value) });
                if (!res.ok) { show('move-error', refusal(res.status, res.body)); return; }
                win.location.reload();
            });

            load();
        }

        // The chat header: breadcrumbs and Move ---------------------------------------

        function initChatHeader() {
            const host = $('project-crumbs-chat');
            if (!host) return;
            const cid = Number(host.dataset.conversationId);
            let context = null;
            call('GET', `/projects/api/conversations/${cid}/context`).then(res => {
                if (!res.ok) return;
                context = res.body;
                host.innerHTML = breadcrumbsHtml(context.path, { current: host.dataset.conversationName || '' });
                host.hidden = !context.path.length;
            });
            const button = $('btn-move-conversation');
            if (!button) return;
            button.addEventListener('click', async () => {
                show('conv-move-error', '');
                $('conv-move-warning').hidden = true;
                const res = await call('GET', '/projects/api');
                const sel = $('conv-move-destination');
                sel.textContent = '';
                const none = node('option', '', 'No project');
                none.value = '';
                sel.appendChild(none);
                (function walk(nodes) {
                    nodes.forEach(n => {
                        const o = node('option', '', `${'  '.repeat(n.depth - 1)}${n.name}`);
                        o.value = n.id;
                        if (context && Number(context.project_id) === Number(n.id)) o.selected = true;
                        sel.appendChild(o);
                        walk(n.children || []);
                    });
                }(res.ok ? res.body.owned : []));
                const m = modal('convMoveModal');
                if (m) m.show();
            });
            async function submit(confirm) {
                const raw = $('conv-move-destination').value;
                const res = await call('POST', `/projects/api/conversations/${cid}/move`, { project_id: raw === '' ? null : Number(raw), confirm });
                if (res.status === 409 && res.body) {
                    const w = moveWarning(res.body);
                    const box = $('conv-move-warning');
                    box.textContent = '';
                    box.appendChild(node('strong', '', 'This move changes who can see the conversation.'));
                    const ul = node('ul', 'mb-0');
                    w.lines.forEach(l => ul.appendChild(node('li', '', l)));
                    box.appendChild(ul);
                    box.hidden = false;
                    $('conv-move-confirm').textContent = 'Move anyway';
                    $('conv-move-confirm').dataset.confirm = '1';
                    return;
                }
                if (!res.ok) { show('conv-move-error', refusal(res.status, res.body)); return; }
                win.location.reload();
            }
            $('conv-move-confirm').addEventListener('click', () => submit($('conv-move-confirm').dataset.confirm === '1'));
            $('conv-move-destination').addEventListener('change', () => {
                $('conv-move-confirm').textContent = 'Move';
                $('conv-move-confirm').dataset.confirm = '';
                $('conv-move-warning').hidden = true;
            });
        }

        // The share picker's limit (PC9) -----------------------------------------------

        logic.pickerLimit = async function pickerLimit(conversationId) {
            const ctx = await call('GET', `/projects/api/conversations/${conversationId}/context`);
            if (!ctx.ok || !ctx.body.path || !ctx.body.path.length) return null;
            const root = ctx.body.path[0].id;
            const members = await call('GET', `/projects/api/${root}/people`);
            if (!members.ok) return { people: null };
            return { people: (members.body.people || []).map(m => ({ id: m.id, name: m.name })) };
        };

        const start = () => { initTree(); initNewProject(); initPage(); initChatHeader(); };
        if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start); else start();
    }

    logic.boot = boot;
    return logic;
}));
