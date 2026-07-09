/* Debate Mode: three-pane SSE UI. */
(function () {
    const root = document.getElementById('debate-root');
    const cid = root.dataset.conversationId;
    let streamId = null;
    let evtSource = null;

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

    function addBlock(role, cls, title, text) {
        const pane = panes[role] || panes.judge;
        const block = el('div', 'mb-2 ' + (cls || ''));
        if (title) block.appendChild(el('div', 'fw-semibold small', title));
        const body = el('div');
        body.innerHTML = md(text);
        block.appendChild(body);
        pane.appendChild(block);
        pane.scrollTop = pane.scrollHeight;
        return block;
    }

    function addExhibits(role, items) {
        (items || []).forEach(ex => {
            const card = el('div', 'exhibit-card');
            card.appendChild(el('div', 'fw-semibold small', `Exhibit ${ex.label}: ${ex.title || ''}`));
            card.appendChild(el('div', 'small', ex.content || ''));
            if (ex.source) card.appendChild(el('div', 'small text-muted', ex.source));
            panes[role].appendChild(card);
        });
        panes[role].scrollTop = panes[role].scrollHeight;
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
                case 'ruling': addBlock('judge', 'debate-ruling', 'Ruling', t.content); break;
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
        return state.config.state;
    }

    function connect() {
        evtSource = new EventSource(`/stream/debate?conversation_id=${cid}`);
        const handlers = {
            stream_start: d => { streamId = d.stream_id; },
            debate_state: d => setStatus(`State: ${d.state}, round ${d.round}`),
            judge_text: d => addBlock('judge', d.phase === 'ruling' ? 'debate-ruling' : '',
                d.phase, d.text),
            debater_turn_start: d => {
                addBlock(d.role, 'text-muted small', '', `Preparing round ${d.round} argument...`);
                document.getElementById(`${d.role}-cancel`).classList.remove('d-none');
            },
            agent_tool_call: d => addBlock(d.role, 'text-muted small', '',
                `Research: ${d.tool_name}`),
            argument: d => {
                addBlock(d.role, '', `Round ${d.round}`, d.text);
                document.getElementById(`${d.role}-cancel`).classList.add('d-none');
            },
            exhibits: d => addExhibits(d.role, d.items),
            turn_failed: d => {
                addBlock(d.role || 'judge', 'text-danger', 'Turn failed', d.error || '');
                document.getElementById('debate-retry').classList.remove('d-none');
            },
            complete: () => {
                setStatus('Debate concluded, ask the judge about the ruling');
                evtSource.close();
            },
            cancelled: () => { setStatus('Paused'); showResume(); evtSource.close(); },
            error: d => { addBlock('judge', 'text-danger', 'Error', d.error || ''); evtSource.close(); },
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
