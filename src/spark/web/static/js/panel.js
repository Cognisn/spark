/* Panel discussions: threaded SSE UI with a speaker rail. */
(function () {
    const root = document.getElementById('panel-root');
    const cid = root.dataset.conversationId;
    const thread = document.getElementById('panel-thread');
    let streamId = null;
    let evtSource = null;
    let qaMode = false;
    let humanPending = null;   // {role, name, round} while it is the user's turn
    let agents = {};           // role -> {display_name, model_id, is_human, ...}
    const currentToolGroup = {};  // role -> live tool group state

    const PALETTE = ['#2a6df4', '#1f7a4d', '#a03030', '#7a4dbf', '#b8860b', '#0f7f8b'];

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    function md(text) {
        // Reuse the app's markdown renderer when available, else plain text
        if (typeof renderMarkdown === 'function') return renderMarkdown(text || '');
        const div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
    }

    function escapeText(text) {
        const div = document.createElement('div');
        div.textContent = text ?? '';
        return div.innerHTML;
    }

    function roleColour(role) {
        if (role === 'moderator') return 'var(--app-accent)';
        const idx = (parseInt(role.split(':')[1], 10) || 1) - 1;
        return PALETTE[idx % PALETTE.length];
    }

    function displayName(role) {
        return (agents[role] && agents[role].display_name) || role;
    }

    function maybeScroll() {
        const box = document.getElementById('panel-autoscroll');
        if (!box || box.checked) {
            // The scroll container is the outer pane card (overflow-y: auto),
            // not the inner thread div, which merely grows.
            const container = document.getElementById('panel-thread-pane');
            container.scrollTop = container.scrollHeight;
        }
    }

    // --- Rail ---------------------------------------------------------------

    function buildRail() {
        const rail = document.getElementById('panel-rail');
        rail.innerHTML = '';
        const roles = ['moderator'].concat(
            Object.keys(agents)
                .filter(r => r.startsWith('panellist:'))
                .sort((a, b) => parseInt(a.split(':')[1], 10) - parseInt(b.split(':')[1], 10))
        );
        roles.forEach(role => {
            const a = agents[role] || {};
            const entry = el('div', 'rail-entry');
            entry.id = `rail-${role.replace(':', '-')}`;
            const chip = el('span', 'rail-chip');
            chip.style.background = roleColour(role);
            entry.appendChild(chip);
            const label = el('div');
            label.appendChild(el('div', 'rail-name', a.display_name || role));
            label.appendChild(el('div', 'rail-sub', a.is_human ? 'You' : (a.model_id || '')));
            entry.appendChild(label);
            rail.appendChild(entry);
        });
    }

    function setFloor(role) {
        document.querySelectorAll('#panel-rail .rail-entry').forEach(e =>
            e.classList.toggle('has-floor', !!role && e.id === `rail-${role.replace(':', '-')}`));
    }

    // --- Thread cards ---------------------------------------------------------

    function addCard(role, title, text, opts) {
        const card = el('div', 'speaker-card' + (role === 'moderator' ? ' moderator-card' : '')
            + ((opts && opts.cls) ? ` ${opts.cls}` : ''));
        card.style.borderLeftColor = roleColour(role || 'moderator');
        if (title) {
            const name = el('div', 'speaker-name', title);
            name.style.color = roleColour(role || 'moderator');
            card.appendChild(name);
        }
        const body = el('div');
        body.innerHTML = md(text);
        card.appendChild(body);
        thread.appendChild(card);
        maybeScroll();
        return card;
    }

    function addMuted(text) {
        const block = el('div', 'text-muted small mb-2', text);
        thread.appendChild(block);
        maybeScroll();
        return block;
    }

    function renderSynthesis(text) {
        const card = el('div', 'judgement-card');
        const title = el('div', 'judgement-title');
        title.innerHTML = '<i class="bi bi-collection me-1"></i> SYNTHESIS';
        const body = el('div');
        body.innerHTML = md(text);
        card.appendChild(title);
        card.appendChild(body);
        thread.appendChild(card);
        maybeScroll();
    }

    function addExhibits(items) {
        (items || []).forEach(ex => {
            const card = el('div', 'exhibit-card');
            card.appendChild(el('div', 'fw-semibold small', `Exhibit ${ex.label}: ${ex.title || ''}`));
            card.appendChild(el('div', 'small', ex.content || ''));
            if (ex.source) card.appendChild(el('div', 'small text-muted', ex.source));
            thread.appendChild(card);
        });
        maybeScroll();
    }

    // --- Tool groups ----------------------------------------------------------

    function startToolGroup(role) {
        const group = el('div', 'tool-group open');
        const header = el('div', 'tool-group-header');
        header.innerHTML = '<i class="bi bi-tools"></i><span class="tool-count">Using tools...</span>' +
            '<i class="bi bi-chevron-down ms-auto"></i>';
        header.addEventListener('click', () => group.classList.toggle('open'));
        const body = el('div', 'tool-group-body');
        group.appendChild(header);
        group.appendChild(body);
        thread.appendChild(group);
        currentToolGroup[role] = { group, body, count: 0, pending: [] };
        maybeScroll();
    }

    function addToolCall(role, toolName) {
        if (!currentToolGroup[role]) startToolGroup(role);
        const state = currentToolGroup[role];
        state.count += 1;
        const entry = el('div', 'tool-entry');
        entry.innerHTML = `<span class="spinner-border spinner-border-sm me-1" style="width:0.7rem;height:0.7rem;"></span>${escapeText(toolName)}`;
        entry.dataset.tool = toolName;
        state.body.appendChild(entry);
        state.pending.push(entry);
        state.group.querySelector('.tool-count').textContent = `Using tools... (${state.count})`;
        maybeScroll();
    }

    function completeToolCall(role, toolName, result, status) {
        const state = currentToolGroup[role];
        if (!state) return;
        const idx = state.pending.findIndex(e => e.dataset.tool === toolName);
        const entry = idx >= 0 ? state.pending.splice(idx, 1)[0] : null;
        if (!entry) return;
        const icon = status === 'success' ? 'bi-check-circle' : 'bi-x-circle';
        entry.innerHTML = `<i class="bi ${icon} me-1"></i>${escapeText(toolName)}` +
            (result ? `<span class="result-snippet">${escapeText(String(result).slice(0, 160))}</span>` : '');
    }

    function closeToolGroup(role) {
        const state = currentToolGroup[role];
        if (!state) return;
        state.group.classList.remove('open');
        state.group.querySelector('.tool-count').textContent =
            `Used ${state.count} tool${state.count === 1 ? '' : 's'}`;
        currentToolGroup[role] = null;
    }

    function addHistoryToolGroup(toolCalls) {
        if (!toolCalls || !toolCalls.length) return;
        const group = el('div', 'tool-group');
        const header = el('div', 'tool-group-header');
        header.innerHTML = `<i class="bi bi-tools"></i><span class="tool-count">Used ${toolCalls.length} tool${toolCalls.length === 1 ? '' : 's'}</span>` +
            '<i class="bi bi-chevron-down ms-auto"></i>';
        header.addEventListener('click', () => group.classList.toggle('open'));
        const body = el('div', 'tool-group-body');
        toolCalls.forEach(tc => {
            const entry = el('div', 'tool-entry');
            entry.innerHTML = `<i class="bi bi-check-circle me-1"></i>${escapeText(tc.name || '')}` +
                (tc.result ? `<span class="result-snippet">${escapeText(String(tc.result).slice(0, 160))}</span>` : '');
            body.appendChild(entry);
        });
        group.appendChild(header);
        group.appendChild(body);
        thread.appendChild(group);
    }

    // --- Status and human mode -------------------------------------------------

    function setStatus(text) {
        document.getElementById('panel-status').textContent = text;
    }

    function showResume() {
        document.getElementById('panel-resume').classList.remove('d-none');
    }

    function enterHumanMode(d) {
        humanPending = d;
        setFloor(d.role);
        const banner = el('div', 'human-banner');
        banner.id = 'panel-human-banner';
        banner.innerHTML = `<i class="bi bi-mic me-1"></i> You have the floor, <strong>${escapeText(d.name)}</strong> — write your round ${d.round} contribution below.`;
        thread.appendChild(banner);
        maybeScroll();
        const input = document.getElementById('panel-prompt');
        input.placeholder = `You have the floor, ${d.name} — write your contribution`;
        document.getElementById('panel-send').textContent = 'Submit contribution';
        setStatus(`Round ${d.round}: your turn`);
        input.focus();
    }

    function exitHumanMode() {
        humanPending = null;
        setFloor(null);
        const banner = document.getElementById('panel-human-banner');
        if (banner) banner.remove();
        const input = document.getElementById('panel-prompt');
        input.placeholder = 'Send a directive to the panel (or a question to the moderator after the synthesis)';
        document.getElementById('panel-send').textContent = 'Send';
    }

    // --- History -----------------------------------------------------------------

    function renderHistory(state) {
        const exhibitsByTurn = state.exhibits || {};
        (state.turns || []).forEach(t => {
            if (t.status !== 'complete') return;
            switch (t.turn_type) {
                case 'announcement': addCard('moderator', 'Moderator — framing', t.content); break;
                case 'interim': addCard('moderator', `Moderator — interim, round ${t.round}`, t.content); break;
                case 'synthesis': renderSynthesis(t.content); break;
                case 'qa_question': addCard('moderator', 'You asked', t.content); break;
                case 'qa_answer': addCard('moderator', 'Moderator', t.content); break;
                case 'user_prompt': addMuted(`User directive: ${t.content}`); break;
                case 'contribution':
                    addCard(t.role, `${displayName(t.role)} — round ${t.round}`, t.content);
                    addExhibits(exhibitsByTurn[String(t.id)]);
                    break;
            }
        });
        setStatus(`State: ${state.config.state}, round ${state.config.current_round}`);
        loadToolHistory();
        return state.config.state;
    }

    async function loadToolHistory() {
        try {
            const resp = await fetch(`/chat/${cid}/api/agent-history`);
            if (!resp.ok) return;
            const data = await resp.json();
            (data.agents || data || []).forEach(run => {
                const name = run.agent_name || '';
                if (!name.endsWith(' panellist')) return;
                let calls = run.tool_calls;
                if (typeof calls === 'string') {
                    try { calls = JSON.parse(calls); } catch (e) { calls = null; }
                }
                addHistoryToolGroup(calls);
            });
        } catch (err) { /* history tool groups are cosmetic */ }
    }

    // --- SSE -----------------------------------------------------------------------

    function connect() {
        evtSource = new EventSource(`/stream/panel?conversation_id=${cid}`);
        const handlers = {
            stream_start: d => { streamId = d.stream_id; },
            panel_state: d => setStatus(`State: ${d.state}, round ${d.round}`),
            moderator_text: d => {
                if (d.phase === 'synthesis') renderSynthesis(d.text);
                else addCard('moderator', `Moderator — ${d.phase}`, d.text);
            },
            panellist_turn_start: d => {
                addMuted(`${d.name} is preparing a round ${d.round} contribution...`);
                startToolGroup(d.role);
                document.getElementById('panel-cancel').classList.remove('d-none');
            },
            agent_tool_call: d => addToolCall(d.role, d.tool_name),
            agent_tool_result: d => completeToolCall(d.role, d.tool_name, d.result, d.status),
            floor: d => setFloor(d.role === 'none' ? null : d.role),
            contribution: d => {
                closeToolGroup(d.role);
                addCard(d.role, `${d.name || displayName(d.role)} — round ${d.round}`, d.text);
                document.getElementById('panel-cancel').classList.add('d-none');
            },
            exhibits: d => addExhibits(d.items),
            human_turn: d => {
                document.getElementById('panel-cancel').classList.add('d-none');
                enterHumanMode(d);
                evtSource.close();
            },
            turn_failed: d => {
                addCard(d.role || 'moderator', 'Turn failed', d.error || '', { cls: 'text-danger' });
                document.getElementById('panel-retry').classList.remove('d-none');
            },
            complete: () => {
                setFloor(null);
                document.getElementById('panel-cancel').classList.add('d-none');
                if (!humanPending) {
                    qaMode = true;
                    setStatus('Panel concluded, ask the moderator about the discussion');
                }
                evtSource.close();
            },
            cancelled: () => { setFloor(null); setStatus('Paused'); showResume(); evtSource.close(); },
            error: d => {
                setFloor(null);
                addCard('moderator', 'Error', d.error || '', { cls: 'text-danger' });
                evtSource.close();
            },
        };
        Object.entries(handlers).forEach(([type, fn]) =>
            evtSource.addEventListener(type, e => fn(e.data ? JSON.parse(e.data) : {})));
        evtSource.onerror = () => {
            if (humanPending) { evtSource.close(); return; }
            setStatus('Disconnected'); showResume(); evtSource.close();
        };
    }

    document.getElementById('panel-resume').addEventListener('click', () => {
        document.getElementById('panel-resume').classList.add('d-none');
        connect();
    });
    document.getElementById('panel-retry').addEventListener('click', () => {
        document.getElementById('panel-retry').classList.add('d-none');
        connect(); // run() resumes and re-attempts the failed turn
    });
    document.getElementById('panel-cancel').addEventListener('click', () => {
        if (streamId) fetch('/stream/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stream_id: streamId }),
        });
    });

    // --- Prompt routing ----------------------------------------------------------

    async function sendPrompt() {
        const input = document.getElementById('panel-prompt');
        const message = input.value.trim();
        if (!message) return;
        input.value = '';

        if (humanPending) {
            const pending = humanPending;
            try {
                const r = await fetch('/panel/api/prompt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ conversation_id: parseInt(cid), message }),
                });
                const data = await r.json();
                if (r.ok && data.accepted === 'contribution') {
                    exitHumanMode();
                    addCard(pending.role, `${pending.name} — round ${pending.round}`, message);
                    connect(); // resume the rotation
                } else {
                    input.value = message; // let the user retry without retyping
                    AppToast.danger('Error', (data && data.error) || 'The contribution was not accepted.');
                }
            } catch (err) {
                input.value = message;
                AppToast.danger('Error', 'Network error.');
            }
            return;
        }

        // Echo immediately so the UI never looks unresponsive.
        if (qaMode) {
            addCard('moderator', 'You asked', message);
            const thinking = addMuted('');
            thinking.innerHTML =
                '<span class="spinner-border spinner-border-sm me-1" ' +
                'style="width:0.7rem;height:0.7rem;"></span>Moderator is considering...';
            setFloor('moderator');
            try {
                const r = await fetch('/panel/api/prompt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ conversation_id: parseInt(cid), message }),
                });
                const data = await r.json();
                thinking.remove();
                if (r.ok && data.answer !== undefined) {
                    addCard('moderator', 'Moderator', data.answer);
                } else {
                    addCard('moderator', 'Error',
                        (data && data.error) || 'The moderator could not answer.', { cls: 'text-danger' });
                }
            } catch (err) {
                thinking.remove();
                addCard('moderator', 'Error', 'Network error.', { cls: 'text-danger' });
            } finally {
                setFloor(null);
            }
            return;
        }

        addMuted(`User directive: ${message}`);
        try {
            const r = await fetch('/panel/api/prompt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ conversation_id: parseInt(cid), message }),
            });
            if (!r.ok) AppToast.danger('Error', 'The directive could not be queued.');
        } catch (err) {
            AppToast.danger('Error', 'Network error.');
        }
    }
    document.getElementById('panel-send').addEventListener('click', sendPrompt);
    document.getElementById('panel-prompt').addEventListener('keydown', e => {
        if (e.key === 'Enter') sendPrompt();
    });

    // Initial load: render history, then connect unless waiting on the user or done.
    fetch(`/panel/api/state?conversation_id=${cid}`)
        .then(r => r.json())
        .then(state => {
            agents = (state.config && state.config.agents) || {};
            buildRail();
            const s = renderHistory(state);
            if (state.awaiting_human) {
                enterHumanMode(state.awaiting_human);
            } else if (s !== 'qa') {
                connect();
            } else {
                qaMode = true;
                setStatus('Panel concluded, ask the moderator about the discussion');
            }
        });
})();
