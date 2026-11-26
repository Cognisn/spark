/**
 * Spark: the project tools' cards in the conversation (PC17, PC18).
 *
 * suggest_project, update_project and move_conversation_to_project answer with a card
 * line ("[spark-project-card] {card, conversation_id, kind}"). The line holds only the
 * card's id: what the card proposes always comes from the server
 * (GET /project-cards/api/<id>), never from the model's text, and only the turn's
 * sender is shown it and can act on it. Every other member of a shared conversation
 * sees the card's kind and state.
 *
 * - Suggestion: the proposed name, description and instructions, with Create, Edit
 *   (change them, then Create) and Dismiss.
 * - Update: each changed field before and after, and the files to add, with Confirm
 *   and Cancel.
 * - Move: where to, and who would gain or lose sight of the conversation, with Confirm
 *   and Cancel. When that changed since the card was shown, the card is shown again.
 *
 * Everything the card shows is set as text, never as markup. chat.js loads this file
 * the first time a card appears and calls SparkProjectCards.fill(slot, resultText).
 * The logic part (no DOM) exports itself under Node for tests.
 */
(function (root) {
    'use strict';

    const MARKER = '[spark-project-card]';
    const TOOLS = ['create_project', 'suggest_project', 'update_project', 'move_conversation_to_project'];
    const KINDS = ['suggestion', 'update', 'move'];
    const API = '/project-cards/api/';
    const STYLES = '/static/css/project_cards.css';
    const FIELD_LABELS = { name: 'Name', description: 'Description', instructions: 'Instructions' };
    const HEADINGS = {
        suggestion: 'Suggested project',
        update: 'Change to a project',
        move: 'Move this conversation',
    };
    const DONE = {
        suggestion: { accepted: 'Created', dismissed: 'Dismissed' },
        update: { accepted: 'Confirmed', cancelled: 'Cancelled' },
        move: { accepted: 'Moved', cancelled: 'Cancelled' },
    };
    const ROLE_LABELS = { owner: 'owner', contributor: 'Contributor', viewer: 'Read-only' };

    // -- Logic ----------------------------------------------------------------------

    function isTool(name) {
        return TOOLS.indexOf(name) !== -1;
    }

    /** The card a project tool's result carries ({card, conversation_id, kind}), or null. */
    function parse(text) {
        const lines = String(text || '').split('\n');
        for (const line of lines) {
            if (!line.startsWith(MARKER)) continue;
            try {
                const data = JSON.parse(line.slice(MARKER.length).trim());
                const ref = {
                    card: String(data.card || ''),
                    conversation_id: Number(data.conversation_id),
                    kind: String(data.kind || ''),
                };
                if (!/^[A-Za-z0-9_-]{16,64}$/.test(ref.card)) return null;
                if (!Number.isInteger(ref.conversation_id) || KINDS.indexOf(ref.kind) === -1) return null;
                return ref;
            } catch (err) {
                return null;
            }
        }
        return null;
    }

    /** The card's URL, or one of its actions' ('accept', 'dismiss'). */
    function cardUrl(id, action) {
        const base = API + encodeURIComponent(id);
        return action ? `${base}/${action === 'accept' ? 'accept' : 'dismiss'}` : base;
    }

    /** The buttons a card offers: none unless its user can still act on it. */
    function actions(view) {
        if (!view || !view.can_act) return [];
        if (view.kind === 'suggestion') {
            return [
                { id: 'create', label: 'Create', action: 'accept', primary: true },
                { id: 'edit', label: 'Edit', action: 'edit' },
                { id: 'dismiss', label: 'Dismiss', action: 'dismiss' },
            ];
        }
        return [
            { id: 'confirm', label: 'Confirm', action: 'accept', primary: true },
            { id: 'cancel', label: 'Cancel', action: 'dismiss' },
        ];
    }

    /** The line under the card's heading: what became of it, or who it waits for. */
    function stateLabel(view) {
        if (!view) return '';
        if (view.state === 'expired') return 'Expired: ask again if you still want this.';
        if (view.state === 'pending') {
            return view.mine ? 'Nothing changes until you choose.' : 'Waiting for the person who asked.';
        }
        const done = (DONE[view.kind] || {})[view.state];
        return done || view.state;
    }

    /** Before-and-after rows for an update card. */
    function diffRows(payload) {
        const before = (payload && payload.before) || {};
        const after = (payload && payload.after) || {};
        return Object.keys(FIELD_LABELS)
            .filter(field => Object.prototype.hasOwnProperty.call(after, field))
            .map(field => ({
                field,
                label: FIELD_LABELS[field],
                before: String(before[field] || ''),
                after: String(after[field] || ''),
            }));
    }

    function _name(payload, guid) {
        const names = (payload && payload.names) || {};
        return String(names[guid] || 'Someone');
    }

    /**
     * A move card's warning: who would newly see the conversation (widen), who would
     * lose sight of it or a role (narrow), whose own shares end. level is 'none' when
     * nobody else is affected.
     */
    function warning(payload) {
        const widened = (payload && payload.widened) || {};
        const narrowed = (payload && payload.narrowed) || {};
        const removed = (payload && payload.shares_removed) || [];
        const lines = [];
        Object.keys(widened).forEach(guid => {
            lines.push(`${_name(payload, guid)} would see it as ${ROLE_LABELS[widened[guid]] || widened[guid]}.`);
        });
        Object.keys(narrowed).forEach(guid => {
            const now = narrowed[guid];
            lines.push(now
                ? `${_name(payload, guid)} would keep it only as ${ROLE_LABELS[now] || now}.`
                : `${_name(payload, guid)} would no longer see it.`);
        });
        removed.forEach(guid => {
            if (!Object.prototype.hasOwnProperty.call(narrowed, guid)) {
                lines.push(`${_name(payload, guid)} would lose the share you gave them.`);
            }
        });
        const narrow = Object.keys(narrowed).length > 0 || removed.length > 0;
        const widen = Object.keys(widened).length > 0;
        const level = widen && narrow ? 'both' : widen ? 'widen' : narrow ? 'narrow' : 'none';
        return { level, lines };
    }

    /** Where a move goes, in words. */
    function destination(payload) {
        const to = payload && payload.to;
        return to ? `Into the project "${String(to.name || '')}"` : 'Out of its project';
    }

    /** The body of an accept request: a suggestion's edits, else nothing. */
    function acceptBody(view, edits) {
        if (!view || view.kind !== 'suggestion' || !edits) return {};
        const out = {};
        Object.keys(FIELD_LABELS).forEach(field => {
            if (typeof edits[field] === 'string') out[field] = edits[field];
        });
        return Object.keys(out).length ? { edits: out } : {};
    }

    const logic = {
        MARKER, TOOLS, isTool, parse, cardUrl, actions, stateLabel, diffRows, warning,
        destination, acceptBody,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = logic;
        return;
    }

    // -- The page -------------------------------------------------------------------

    function ensureStyles() {
        if (document.getElementById('project-cards-css')) return;
        const link = document.createElement('link');
        link.id = 'project-cards-css';
        link.rel = 'stylesheet';
        link.href = STYLES;
        document.head.appendChild(link);
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function block(label, value) {
        const wrap = el('div', 'project-card-field');
        wrap.appendChild(el('div', 'project-card-label', label));
        wrap.appendChild(el('div', 'project-card-value', value || '(none)'));
        return wrap;
    }

    function body(view) {
        const out = el('div', 'project-card-body');
        const p = view.payload;
        if (!p) return out;
        if (view.kind === 'suggestion') {
            out.appendChild(block('Name', p.name));
            if (p.description) out.appendChild(block('Description', p.description));
            if (p.instructions) out.appendChild(block('Instructions', p.instructions));
            if (p.reason) out.appendChild(el('p', 'project-card-reason', p.reason));
        } else if (view.kind === 'update') {
            out.appendChild(el('p', 'project-card-target', `Project "${String((p.project || {}).name || '')}"`));
            diffRows(p).forEach(row => {
                const wrap = el('div', 'project-card-diff');
                wrap.appendChild(el('div', 'project-card-label', row.label));
                const pair = el('div', 'project-card-pair');
                pair.appendChild(block('Before', row.before));
                pair.appendChild(block('After', row.after));
                wrap.appendChild(pair);
                out.appendChild(wrap);
            });
            if ((p.files || []).length) {
                const list = el('ul', 'project-card-files');
                p.files.forEach(f => list.appendChild(el('li', '', String(f.name || `File ${f.file_id}`))));
                out.appendChild(el('div', 'project-card-label', 'Add as resources'));
                out.appendChild(list);
            }
        } else {
            out.appendChild(el('p', 'project-card-target', destination(p)));
            const warn = warning(p);
            const note = el('div', `project-card-warning project-card-warning-${warn.level}`);
            note.setAttribute('role', warn.level === 'none' ? 'note' : 'alert');
            if (warn.level === 'none') {
                note.textContent = 'Nobody else would gain or lose sight of this conversation.';
            } else {
                note.appendChild(el('div', 'project-card-label', 'Who can see this conversation changes'));
                const list = el('ul');
                warn.lines.forEach(line => list.appendChild(el('li', '', line)));
                note.appendChild(list);
            }
            out.appendChild(note);
        }
        return out;
    }

    function editor(view, card) {
        const form = el('form', 'project-card-editor');
        const p = view.payload || {};
        Object.keys(FIELD_LABELS).forEach(field => {
            const label = el('label', 'project-card-label', FIELD_LABELS[field]);
            const input = document.createElement(field === 'name' ? 'input' : 'textarea');
            input.className = 'form-control form-control-sm';
            input.name = field;
            input.value = String(p[field] || '');
            if (field !== 'name') input.rows = field === 'instructions' ? 5 : 2;
            label.appendChild(input);
            form.appendChild(label);
        });
        const row = el('div', 'project-card-actions');
        const create = el('button', 'btn btn-sm btn-primary', 'Create');
        create.type = 'submit';
        const back = el('button', 'btn btn-sm btn-outline-secondary', 'Back');
        back.type = 'button';
        back.addEventListener('click', () => render(card, view));
        row.appendChild(create);
        row.appendChild(back);
        form.appendChild(row);
        form.addEventListener('submit', event => {
            event.preventDefault();
            const edits = {};
            Object.keys(FIELD_LABELS).forEach(field => { edits[field] = form.elements[field].value; });
            act(card, view, 'accept', edits);
        });
        return form;
    }

    function render(card, view, message) {
        card.innerHTML = '';
        card.dataset.state = view.state;
        card.dataset.kind = view.kind;
        const header = el('div', 'project-card-header');
        header.appendChild(el('i', `bi ${view.kind === 'move' ? 'bi-folder-symlink' : 'bi-folder-plus'} project-card-icon`));
        header.appendChild(el('span', 'project-card-title', HEADINGS[view.kind] || 'Project'));
        card.appendChild(header);
        card.appendChild(el('div', 'project-card-state', stateLabel(view)));
        if (view.mine) card.appendChild(body(view));
        if (message) {
            const note = el('div', 'project-card-message', message);
            note.setAttribute('role', 'status');
            card.appendChild(note);
        }
        const buttons = actions(view);
        if (buttons.length) {
            const row = el('div', 'project-card-actions');
            buttons.forEach(b => {
                const button = el('button', `btn btn-sm ${b.primary ? 'btn-primary' : 'btn-outline-secondary'}`, b.label);
                button.type = 'button';
                button.dataset.action = b.id;
                button.addEventListener('click', () => {
                    if (b.action === 'edit') {
                        card.replaceChild(editor(view, card), card.querySelector('.project-card-actions'));
                        return;
                    }
                    act(card, view, b.action);
                });
                row.appendChild(button);
            });
            card.appendChild(row);
        }
    }

    async function act(card, view, action, edits) {
        card.querySelectorAll('button').forEach(b => { b.disabled = true; });
        try {
            const resp = await fetch(cardUrl(view.id, action), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(action === 'accept' ? acceptBody(view, edits) : {}),
            });
            const data = await resp.json().catch(() => ({}));
            const next = data.card || view;
            render(card, next, resp.ok ? '' : String(data.error || 'That did not work.'));
        } catch (err) {
            render(card, view, 'That did not work. Check the connection and try again.');
        }
    }

    /** Fill ``slot`` (a message wrapper chat.js placed) with the card ``text`` carries. */
    async function fill(slot, text) {
        const ref = parse(text);
        if (!slot || !ref) {
            if (slot && slot.parentNode) slot.parentNode.removeChild(slot);
            return null;
        }
        ensureStyles();
        const card = el('section', 'project-card');
        card.setAttribute('aria-label', HEADINGS[ref.kind]);
        card.appendChild(el('div', 'project-card-state', 'Loading'));
        slot.appendChild(card);
        try {
            const resp = await fetch(cardUrl(ref.card), { headers: { Accept: 'application/json' } });
            if (!resp.ok) throw new Error(String(resp.status));
            const data = await resp.json();
            render(card, data.card);
        } catch (err) {
            card.textContent = 'This card is no longer available.';
        }
        return card;
    }

    root.SparkProjectCards = Object.assign({}, logic, { fill, render });
})(typeof window !== 'undefined' ? window : this);
