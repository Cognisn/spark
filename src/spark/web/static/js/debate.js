/* Debate Mode: three-pane SSE UI. */
(function () {
    const root = document.getElementById('debate-root');
    const cid = root.dataset.conversationId;
    let streamId = null;
    let evtSource = null;
    const currentToolGroup = { pro: null, con: null };

    const panes = {
        judge: document.getElementById('judge-content'),
        pro: document.getElementById('pro-content'),
        con: document.getElementById('con-content'),
    };

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

    function autoScrollEnabled(role) {
        const box = document.getElementById(`${role}-autoscroll`);
        return !box || box.checked;
    }

    function maybeScroll(role) {
        if (autoScrollEnabled(role)) {
            const pane = panes[role] || panes.judge;
            pane.scrollTop = pane.scrollHeight;
        }
    }

    function addBlock(role, cls, title, text) {
        const pane = panes[role] || panes.judge;
        const block = el('div', 'mb-2 ' + (cls || ''));
        if (title) block.appendChild(el('div', 'fw-semibold small', title));
        const body = el('div');
        body.innerHTML = md(text);
        block.appendChild(body);
        pane.appendChild(block);
        maybeScroll(role);
        return block;
    }

    function renderJudgement(text) {
        const card = el('div', 'judgement-card');
        const title = el('div', 'judgement-title');
        title.innerHTML = '<i class="bi bi-hammer me-1"></i> FINAL JUDGEMENT';
        const body = el('div');
        body.innerHTML = md(text);
        card.appendChild(title);
        card.appendChild(body);
        panes.judge.appendChild(card);
        maybeScroll('judge');
    }

    function startToolGroup(role) {
        const group = el('div', 'tool-group open');
        const header = el('div', 'tool-group-header');
        header.innerHTML = '<i class="bi bi-tools"></i><span class="tool-count">Using tools...</span>' +
            '<i class="bi bi-chevron-down ms-auto"></i>';
        header.addEventListener('click', () => group.classList.toggle('open'));
        const body = el('div', 'tool-group-body');
        group.appendChild(header);
        group.appendChild(body);
        panes[role].appendChild(group);
        currentToolGroup[role] = { group, body, count: 0, pending: [] };
        maybeScroll(role);
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
        maybeScroll(role);
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

    function addHistoryToolGroup(role, toolCalls) {
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
        panes[role].appendChild(group);
    }

    function escapeText(text) {
        const div = document.createElement('div');
        div.textContent = text ?? '';
        return div.innerHTML;
    }

    function setFloor(role) {
        ['judge', 'pro', 'con'].forEach(r => {
            document.getElementById(`${r}-pane`).classList.toggle('has-floor', r === role);
        });
    }

    function addExhibits(role, items) {
        (items || []).forEach(ex => {
            const card = el('div', 'exhibit-card');
            card.appendChild(el('div', 'fw-semibold small', `Exhibit ${ex.label}: ${ex.title || ''}`));
            card.appendChild(el('div', 'small', ex.content || ''));
            if (ex.source) card.appendChild(el('div', 'small text-muted', ex.source));
            panes[role].appendChild(card);
        });
        maybeScroll(role);
    }

    function setStatus(text) {
        document.getElementById('debate-status').textContent = text;
    }

    function showResume() {
        document.getElementById('debate-resume').classList.remove('d-none');
    }

    function renderHistory(state) {
        const exhibitsByTurn = state.exhibits || {};
        (state.turns || []).forEach(t => {
            if (t.status !== 'complete') return;
            switch (t.turn_type) {
                case 'announcement': addBlock('judge', '', 'Opening', t.content); break;
                case 'interim': addBlock('judge', '', `Interim, round ${t.round}`, t.content); break;
                case 'ruling': renderJudgement(t.content); break;
                case 'qa_question': addBlock('judge', '', 'You asked', t.content); break;
                case 'qa_answer': addBlock('judge', '', 'Judge', t.content); break;
                case 'user_prompt':
                    ['judge', 'pro', 'con'].forEach(r =>
                        addBlock(r, 'text-muted small', 'User directive', t.content));
                    break;
                case 'argument':
                    addBlock(t.role, '', `Round ${t.round}`, t.content);
                    addExhibits(t.role, exhibitsByTurn[String(t.id)]);
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
                const name = (run.agent_name || '').toLowerCase();
                const role = name.startsWith('pro') ? 'pro' : name.startsWith('con') ? 'con' : null;
                if (!role) return;
                let calls = run.tool_calls;
                if (typeof calls === 'string') {
                    try { calls = JSON.parse(calls); } catch (e) { calls = null; }
                }
                addHistoryToolGroup(role, calls);
            });
        } catch (err) { /* history tool groups are cosmetic */ }
    }

    function connect() {
        evtSource = new EventSource(`/stream/debate?conversation_id=${cid}`);
        const handlers = {
            stream_start: d => { streamId = d.stream_id; },
            debate_state: d => setStatus(`State: ${d.state}, round ${d.round}`),
            judge_text: d => {
                if (d.phase === 'ruling') renderJudgement(d.text);
                else addBlock('judge', '', d.phase, d.text);
            },
            debater_turn_start: d => {
                addBlock(d.role, 'text-muted small', '', `Preparing round ${d.round} argument...`);
                startToolGroup(d.role);
                document.getElementById(`${d.role}-cancel`).classList.remove('d-none');
            },
            agent_tool_call: d => addToolCall(d.role, d.tool_name),
            agent_tool_result: d => completeToolCall(d.role, d.tool_name, d.result, d.status),
            floor: d => setFloor(d.role === 'none' ? null : d.role),
            argument: d => {
                closeToolGroup(d.role);
                addBlock(d.role, '', `Round ${d.round}`, d.text);
                document.getElementById(`${d.role}-cancel`).classList.add('d-none');
            },
            exhibits: d => addExhibits(d.role, d.items),
            turn_failed: d => {
                addBlock(d.role || 'judge', 'text-danger', 'Turn failed', d.error || '');
                document.getElementById('debate-retry').classList.remove('d-none');
            },
            complete: () => {
                setFloor(null);
                setStatus('Debate concluded, ask the judge about the ruling');
                evtSource.close();
            },
            cancelled: () => { setFloor(null); setStatus('Paused'); showResume(); evtSource.close(); },
            error: d => { setFloor(null); addBlock('judge', 'text-danger', 'Error', d.error || ''); evtSource.close(); },
        };
        Object.entries(handlers).forEach(([type, fn]) =>
            evtSource.addEventListener(type, e => fn(e.data ? JSON.parse(e.data) : {})));
        evtSource.onerror = () => { setStatus('Disconnected'); showResume(); evtSource.close(); };
    }

    document.getElementById('debate-resume').addEventListener('click', () => {
        document.getElementById('debate-resume').classList.add('d-none');
        connect();
    });
    document.getElementById('debate-retry').addEventListener('click', () => {
        document.getElementById('debate-retry').classList.add('d-none');
        connect(); // run() resumes and re-attempts the failed turn
    });
    ['pro', 'con'].forEach(role => {
        document.getElementById(`${role}-cancel`).addEventListener('click', () => {
            if (streamId) fetch('/stream/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stream_id: streamId }),
            });
        });
    });

    async function sendPrompt() {
        const input = document.getElementById('debate-prompt');
        const message = input.value.trim();
        if (!message) return;
        input.value = '';
        const r = await fetch('/debate/api/prompt', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ conversation_id: parseInt(cid), message }),
        });
        const data = await r.json();
        if (data.answer !== undefined) {
            addBlock('judge', '', 'You asked', message);
            addBlock('judge', '', 'Judge', data.answer);
        } else {
            ['judge', 'pro', 'con'].forEach(role =>
                addBlock(role, 'text-muted small', 'User directive', message));
        }
    }
    document.getElementById('debate-send').addEventListener('click', sendPrompt);
    document.getElementById('debate-prompt').addEventListener('keydown', e => {
        if (e.key === 'Enter') sendPrompt();
    });

    // Initial load: render history, then connect unless already concluded.
    fetch(`/debate/api/state?conversation_id=${cid}`)
        .then(r => r.json())
        .then(state => {
            const s = renderHistory(state);
            if (s !== 'qa') connect();
            else setStatus('Debate concluded, ask the judge about the ruling');
        });
})();
