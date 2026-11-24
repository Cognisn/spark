/**
 * Spark: shared conversations on the chat page (server mode).
 *
 * window.SparkSharing knows the signed-in user's role in the conversation, its
 * people and their colours, and who holds the turn. It
 *   - labels each message with its sender's name, initials and colour, and an
 *     assistant reply with whose turn it answered (chat.js asks it);
 *   - keeps the composer closed while the user may not send (read-only, a model
 *     they may not use) or while another participant's turn runs;
 *   - follows the conversation live (GET /stream/conversation): another person's
 *     turn streams in as it happens and the history reloads when it ends; a
 *     removal or deletion closes the page's view at once;
 *   - drives the Share dialog for the owner: people picker, roles, removal and
 *     transfer.
 */
(function () {
    'use strict';

    const state = {
        conversationId: null,
        enabled: false,
        role: 'owner',
        maySend: true,
        refusal: null,
        members: [],
        byGuid: {},
        me: null,
        turn: null,
        watch: null,
        liveBubble: null,
        liveText: '',
        liveStream: null,
    };

    function esc(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function jsonHeaders() {
        return { 'Content-Type': 'application/json', Accept: 'application/json' };
    }

    // -- People and labels ------------------------------------------------------------

    function setMembers(members) {
        state.members = members || [];
        state.byGuid = {};
        state.members.forEach(m => {
            state.byGuid[m.user_guid] = m;
            if (m.is_me) state.me = m.user_guid;
        });
    }

    function isShared() {
        return state.enabled && state.members.length > 1;
    }

    /** The participant a message belongs to: {name, initials, colour, isMe}, or null. */
    function participantOf(msg) {
        if (!isShared() || !msg) return null;
        const guid = msg.user_guid || '';
        const member = state.byGuid[guid];
        const sender = msg.sender || {};
        if (!member && !sender.name) return null;
        return {
            name: member ? member.name : sender.name,
            initials: member ? member.initials : (sender.initials || '?'),
            colour: member ? member.colour : 0,
            isMe: guid === state.me,
        };
    }

    /** The header HTML of a message bubble in a shared conversation, or null (use the usual). */
    function headerFor(role, msg) {
        const who = participantOf(msg);
        if (!who) return null;
        const cls = who.colour ? `participant-${who.colour}` : '';
        if (role === 'assistant') {
            return `
                <div class="message-header">
                    <span class="d-flex align-items-center gap-1">
                        <i class="bi bi-robot" style="color: var(--app-accent);" aria-hidden="true"></i>
                        <strong style="font-size: 0.8125rem;">Spark</strong>
                        <span class="replying-to ${cls}">Replying to <span class="participant-name">${esc(who.name)}</span></span>
                    </span>
                </div>`;
        }
        const label = who.isMe ? `${esc(who.name)} (you)` : esc(who.name);
        return `
            <div class="message-header">
                <span class="d-flex align-items-center gap-1 ${cls}">
                    <span class="participant-initials" aria-hidden="true">${esc(who.initials)}</span>
                    <span class="participant-name">${label}</span>
                </span>
            </div>`;
    }

    /** The colour class of a message bubble (user messages carry the sender's colour). */
    function bubbleClass(role, msg) {
        const who = participantOf(msg);
        if (!who || !who.colour) return '';
        return `participant participant-${who.colour}`;
    }

    // -- The composer -----------------------------------------------------------------

    function setComposer() {
        const input = document.getElementById('chat-input');
        const button = document.getElementById('btn-send');
        const waiting = document.getElementById('turn-waiting');
        if (!input || !button) return;
        const ownStream = typeof currentStreamId !== 'undefined' && currentStreamId;
        const otherTurn = state.turn && state.turn.user_guid !== state.me && !ownStream;
        let reason = null;
        if (!state.maySend) reason = state.refusal || 'You have read-only access to this conversation.';
        else if (otherTurn) reason = `Waiting for ${state.turn.name}'s turn to finish`;
        input.disabled = !!reason;
        if (!ownStream) button.disabled = !!reason;
        input.placeholder = reason || 'Type a message...';
        if (waiting) {
            waiting.textContent = reason || '';
            waiting.hidden = !reason;
        }
    }

    // -- Header: the sharing icon and owner-only controls --------------------------------

    function setHeader() {
        const indicator = document.getElementById('shared-indicator');
        if (indicator) {
            indicator.hidden = !isShared();
            const names = state.members.map(m => `${m.name} (${m.role === 'viewer' ? 'read-only' : m.role})`);
            indicator.title = `Shared with: ${names.join(', ')}`;
            indicator.setAttribute('aria-label', indicator.title);
            const people = document.getElementById('shared-people');
            if (people) {
                people.innerHTML = state.members.map(m =>
                    `<span class="participant-initials participant-${m.colour}" title="${esc(m.name)}">${esc(m.initials)}</span>`
                ).join('');
            }
        }
        const owner = state.role === 'owner';
        document.querySelectorAll('[data-owner-only]').forEach(el => { el.hidden = !owner; });
        const share = document.getElementById('btn-share');
        if (share) share.hidden = !(state.enabled && owner);
    }

    async function refresh() {
        if (!state.conversationId) return;
        try {
            const resp = await fetch(`/chat/${state.conversationId}/api/info`, { headers: { Accept: 'application/json' } });
            if (resp.status === 404) return closeView('removed');
            if (!resp.ok) return;
            const info = await resp.json();
            state.role = info.my_role || 'owner';
            state.maySend = info.may_send !== false;
            state.refusal = info.send_refusal || null;
            state.enabled = !!info.sharing;
            state.turn = info.turn || null;
            setMembers(info.members);
            setHeader();
            setComposer();
            if (document.getElementById('shareModal') && document.getElementById('shareModal').classList.contains('show')) {
                renderMembers();
            }
        } catch (err) {
            // The page keeps working; the next event refreshes again.
        }
    }

    // -- Following the conversation live ----------------------------------------------

    function liveBubble(name) {
        if (state.liveBubble && document.body.contains(state.liveBubble)) return state.liveBubble;
        const container = document.getElementById('chat-messages');
        if (!container) return null;
        const wrapper = document.createElement('div');
        wrapper.className = 'chat-message-wrapper assistant';
        wrapper.id = 'live-turn';
        wrapper.innerHTML = `
            <div class="chat-message assistant">
                <div class="message-header"><span class="d-flex align-items-center gap-1">
                    <i class="bi bi-robot" style="color: var(--app-accent);" aria-hidden="true"></i>
                    <strong style="font-size: 0.8125rem;">Spark</strong>
                    <span class="replying-to">Replying to ${esc(name)}</span>
                </span></div>
                <div class="markdown-content" aria-live="polite"><em>${esc(name)} is waiting for a reply…</em></div>
            </div>`;
        container.appendChild(wrapper);
        state.liveBubble = wrapper;
        return wrapper;
    }

    function isMine(data) {
        return typeof currentStreamId !== 'undefined' && currentStreamId && data.stream_id === currentStreamId;
    }

    function onEvent(kind, data) {
        if (kind === 'ready') {
            state.turn = data.turn || null;
            setComposer();
            return;
        }
        if (kind === 'turn_started') {
            state.turn = { user_guid: data.user_guid, name: data.name, stream_id: data.stream_id };
            setComposer();
            if (!isMine(data) && data.user_guid !== state.me) {
                state.liveText = '';
                liveBubble(data.name);
                if (typeof scrollToBottom === 'function') scrollToBottom();
            }
            return;
        }
        if (kind === 'delta') {
            if (isMine(data) || !state.turn || state.turn.user_guid === state.me) return;
            state.liveText += data.text || '';
            const bubble = liveBubble(state.turn.name);
            const body = bubble && bubble.querySelector('.markdown-content');
            if (body) body.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(state.liveText) : esc(state.liveText);
            if (typeof scrollToBottom === 'function') scrollToBottom();
            return;
        }
        if (kind === 'tool') {
            if (isMine(data) || !state.turn || state.turn.user_guid === state.me) return;
            const bubble = liveBubble(state.turn.name);
            const body = bubble && bubble.querySelector('.markdown-content');
            if (body && data.phase === 'approval') {
                body.innerHTML = `<em>Waiting for ${esc(state.turn.name)} to approve a tool</em>`;
            } else if (body && data.phase === 'start' && !state.liveText) {
                body.innerHTML = `<em>Using ${esc(data.tool_name)}…</em>`;
            }
            return;
        }
        if (kind === 'turn_finished') {
            const mine = isMine(data) || data.user_guid === state.me;
            state.turn = null;
            if (state.liveBubble) { state.liveBubble.remove(); state.liveBubble = null; }
            state.liveText = '';
            setComposer();
            if (!mine && typeof loadChatHistory === 'function') loadChatHistory(state.conversationId);
            return;
        }
        if (kind === 'members_changed') {
            refresh();
            return;
        }
        if (kind === 'removed' || kind === 'deleted') {
            closeView(kind);
        }
    }

    function closeView(kind) {
        if (state.watch) { state.watch.close(); state.watch = null; }
        const input = document.getElementById('chat-input');
        const button = document.getElementById('btn-send');
        if (input) { input.disabled = true; input.placeholder = ''; }
        if (button) button.disabled = true;
        const text = kind === 'deleted'
            ? 'This conversation was deleted by its owner.'
            : 'You no longer have access to this conversation.';
        const container = document.getElementById('chat-messages');
        if (container) {
            container.innerHTML = `<div class="alert-app alert-app-danger" role="alert">${text}
                <a href="/conversations">Back to conversations</a></div>`;
        }
    }

    function watch() {
        if (!state.enabled || state.watch || !window.EventSource) return;
        const source = new EventSource(`/stream/conversation?conversation_id=${state.conversationId}`);
        ['ready', 'turn_started', 'delta', 'tool', 'turn_finished', 'members_changed', 'member_removed', 'removed', 'deleted']
            .forEach(kind => source.addEventListener(kind, e => {
                let data = {};
                try { data = JSON.parse(e.data || '{}'); } catch (err) { data = {}; }
                onEvent(kind, data);
            }));
        source.onerror = () => {
            // The browser reconnects by itself; a refused reconnect means access is gone.
            if (source.readyState === EventSource.CLOSED) {
                state.watch = null;
                refresh();
            }
        };
        state.watch = source;
    }

    // -- The Share dialog -------------------------------------------------------------

    const ROLES = [['contributor', 'Contributor'], ['viewer', 'Read-only']];

    function roleSelect(current, id) {
        return `<select class="form-select form-select-sm" style="width: auto;" id="${id}" aria-label="Role">` +
            ROLES.map(([v, label]) => `<option value="${v}"${v === current ? ' selected' : ''}>${label}</option>`).join('') +
            '</select>';
    }

    function renderMembers() {
        const box = document.getElementById('share-members');
        if (!box) return;
        box.innerHTML = state.members.map(m => {
            const name = `<span class="participant-initials participant-${m.colour}" aria-hidden="true">${esc(m.initials)}</span>
                <span class="share-person-name">${esc(m.name)}${m.is_me ? ' (you)' : ''}</span>`;
            if (m.role === 'owner') {
                return `<div class="share-person">${name}<span class="badge text-bg-secondary">Owner</span></div>`;
            }
            const sel = `member-role-${m.user_guid}`;
            return `<div class="share-person" data-guid="${esc(m.user_guid)}">${name}
                ${roleSelect(m.role, sel)}
                <button type="button" class="btn btn-app-ghost btn-sm" data-act="transfer">Make owner</button>
                <button type="button" class="btn btn-app-ghost btn-sm" style="color: var(--app-danger);" data-act="remove" aria-label="Remove ${esc(m.name)}">Remove</button>
            </div>`;
        }).join('');
        box.querySelectorAll('.share-person[data-guid]').forEach(row => {
            const guid = row.dataset.guid;
            const select = row.querySelector('select');
            if (select) select.addEventListener('change', () => changeRole(guid, select.value));
            row.querySelector('[data-act="remove"]').addEventListener('click', () => removeMember(guid));
            row.querySelector('[data-act="transfer"]').addEventListener('click', () => transferTo({ user_guid: guid }));
        });
    }

    async function post(url, body) {
        const resp = await fetch(url, { method: 'POST', headers: jsonHeaders(), body: JSON.stringify(body) });
        let data = {};
        try { data = await resp.json(); } catch (err) { data = {}; }
        if (!resp.ok) throw new Error(data.error || 'The change was not saved.');
        return data;
    }

    function toast(kind, title, text) {
        if (window.AppToast && AppToast[kind]) AppToast[kind](title, text);
    }

    async function addPerson(userId, role) {
        try {
            const data = await post(`/conversations/api/${state.conversationId}/members`, { user_id: userId, role });
            setMembers(data.members);
            renderMembers();
            setHeader();
            document.getElementById('share-search').value = '';
            document.getElementById('share-results').innerHTML = '';
            toast('success', 'Shared', 'They can open it from Shared with me.');
        } catch (err) {
            toast('danger', 'Not shared', err.message);
        }
    }

    async function changeRole(guid, role) {
        try {
            const data = await post(`/conversations/api/${state.conversationId}/members`, { user_guid: guid, role });
            setMembers(data.members);
            renderMembers();
        } catch (err) {
            toast('danger', 'Not changed', err.message);
        }
    }

    async function removeMember(guid) {
        const member = state.byGuid[guid];
        if (!confirm(`Remove ${member ? member.name : 'this person'}? They lose access at once.`)) return;
        const resp = await fetch(`/conversations/api/${state.conversationId}/members/${encodeURIComponent(guid)}`, { method: 'DELETE' });
        if (!resp.ok) { toast('danger', 'Not removed', 'The change was not saved.'); return; }
        await refresh();
        renderMembers();
    }

    async function transferTo(target) {
        const member = target.user_guid ? state.byGuid[target.user_guid] : null;
        const name = member ? member.name : (target.name || 'this person');
        if (!confirm(`Make ${name} the owner? They take over sharing, settings and deletion, and you become a contributor.`)) return;
        try {
            await post(`/conversations/api/${state.conversationId}/transfer`, target);
            toast('success', 'Transferred', `${name} is now the owner.`);
            const modal = window.bootstrap && bootstrap.Modal.getInstance(document.getElementById('shareModal'));
            if (modal) modal.hide();
            await refresh();
        } catch (err) {
            toast('danger', 'Not transferred', err.message);
        }
    }

    let searchTimer = null;

    function search(query) {
        clearTimeout(searchTimer);
        const box = document.getElementById('share-results');
        if (query.trim().length < 2) { box.innerHTML = ''; return; }
        searchTimer = setTimeout(async () => {
            const resp = await fetch(`/conversations/api/people?q=${encodeURIComponent(query.trim())}`, { headers: { Accept: 'application/json' } });
            const people = resp.ok ? await resp.json() : [];
            const known = new Set(state.members.map(m => m.name));
            if (!people.length) { box.innerHTML = '<div class="form-text">No one found.</div>'; return; }
            box.innerHTML = people.map((p, i) => `
                <div class="share-person" data-id="${esc(p.id)}">
                    <span class="share-person-name">${esc(p.name)}<br><small style="color: var(--app-text-muted);">${esc(p.email)}</small></span>
                    ${roleSelect('viewer', `new-role-${i}`)}
                    <button type="button" class="btn btn-app-primary btn-sm" data-act="add"${known.has(p.name) ? ' disabled' : ''}>Add</button>
                    <button type="button" class="btn btn-app-ghost btn-sm" data-act="transfer">Make owner</button>
                </div>`).join('');
            box.querySelectorAll('.share-person[data-id]').forEach(row => {
                const id = row.dataset.id;
                row.querySelector('[data-act="add"]').addEventListener('click', () => addPerson(id, row.querySelector('select').value));
                row.querySelector('[data-act="transfer"]').addEventListener('click', () =>
                    transferTo({ user_id: id, name: row.querySelector('.share-person-name').firstChild.textContent }));
            });
        }, 250);
    }

    function openDialog() {
        renderMembers();
        const el = document.getElementById('shareModal');
        if (el && window.bootstrap) bootstrap.Modal.getOrCreateInstance(el).show();
    }

    // -- Start --------------------------------------------------------------------------

    async function init(options) {
        state.conversationId = options.conversationId;
        state.enabled = !!options.sharing;
        state.role = options.role || 'owner';
        const share = document.getElementById('btn-share');
        if (share) share.addEventListener('click', openDialog);
        const input = document.getElementById('share-search');
        if (input) input.addEventListener('input', () => search(input.value));
        setHeader();
        if (!state.enabled) return;
        await refresh();
        watch();
    }

    window.SparkSharing = {
        init,
        mine: () => ({ user_guid: state.me }),
        refresh,
        headerFor,
        bubbleClass,
        setComposer,
        isShared,
        _onEvent: onEvent,
    };
})();
