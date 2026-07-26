/* Knowledge page: management + custom canvas force-layout visualisation. */
(function () {
    const TYPE_COLOURS = {
        person: '#4f9cf9', organisation: '#8b5cf6', project: '#22c1a8',
        system: '#f59e0b', concept: '#64748b', place: '#10b981',
        event: '#ef4444', other: '#94a3b8',
    };

    const canvas = document.getElementById('kg-canvas');
    const ctx = canvas.getContext('2d');
    let nodes = [], edges = [], nodeById = {};
    let scale = 1, panX = 0, panY = 0;
    let dragNode = null, panning = false, lastX = 0, lastY = 0;
    let selected = null, running = false;

    function fitCanvas() {
        const rect = canvas.getBoundingClientRect();
        canvas.width = rect.width * devicePixelRatio;
        canvas.height = rect.height * devicePixelRatio;
        ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    }
    window.addEventListener('resize', () => { fitCanvas(); draw(); });

    // ---- Force simulation -------------------------------------------------
    function simulate() {
        if (!running) return;
        let movement = 0;
        const w = canvas.clientWidth, h = canvas.clientHeight;
        for (const n of nodes) {
            if (n === dragNode) continue;
            let fx = (w / 2 - n.x) * 0.0008, fy = (h / 2 - n.y) * 0.0008; // gravity
            for (const m of nodes) {
                if (m === n) continue;
                const dx = n.x - m.x, dy = n.y - m.y;
                const d2 = Math.max(100, dx * dx + dy * dy);
                const rep = 1200 / d2;
                fx += dx * rep / Math.sqrt(d2);
                fy += dy * rep / Math.sqrt(d2);
            }
            n.vx = (n.vx + fx) * 0.85;
            n.vy = (n.vy + fy) * 0.85;
        }
        for (const e of edges) {
            const a = nodeById[e.source_node_id], b = nodeById[e.target_node_id];
            if (!a || !b) continue;
            const dx = b.x - a.x, dy = b.y - a.y;
            const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
            const pull = (dist - 90) * 0.002;
            if (a !== dragNode) { a.vx += dx / dist * pull; a.vy += dy / dist * pull; }
            if (b !== dragNode) { b.vx -= dx / dist * pull; b.vy -= dy / dist * pull; }
        }
        for (const n of nodes) {
            if (n === dragNode) continue;
            n.x += n.vx; n.y += n.vy;
            movement += Math.abs(n.vx) + Math.abs(n.vy);
        }
        draw();
        if (movement > 0.5) requestAnimationFrame(simulate);
        else running = false;
    }

    function kick() { if (!running) { running = true; requestAnimationFrame(simulate); } }

    // ---- Rendering --------------------------------------------------------
    function radius(n) { return 4 + 2 * Math.log(1 + (n.weight || 1)); }

    function draw() {
        const w = canvas.clientWidth, h = canvas.clientHeight;
        ctx.clearRect(0, 0, w, h);
        ctx.save();
        ctx.translate(panX, panY);
        ctx.scale(scale, scale);

        const line = getComputedStyle(document.documentElement)
            .getPropertyValue('--app-border').trim() || '#8884';
        for (const e of edges) {
            const a = nodeById[e.source_node_id], b = nodeById[e.target_node_id];
            if (!a || !b) continue;
            ctx.strokeStyle = line;
            ctx.globalAlpha = Math.min(0.9, 0.25 + 0.1 * (e.weight || 1));
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
        ctx.globalAlpha = 1;

        const labelColour = getComputedStyle(document.documentElement)
            .getPropertyValue('--app-text-primary').trim() || '#888';
        const labelled = [...nodes].sort((a, b) => (b.weight || 1) - (a.weight || 1)).slice(0, 30);
        for (const n of nodes) {
            ctx.fillStyle = TYPE_COLOURS[n.entity_type] || TYPE_COLOURS.other;
            ctx.beginPath(); ctx.arc(n.x, n.y, radius(n), 0, Math.PI * 2); ctx.fill();
            if (n === selected) {
                ctx.strokeStyle = labelColour; ctx.lineWidth = 2;
                ctx.stroke(); ctx.lineWidth = 1;
            }
        }
        ctx.fillStyle = labelColour;
        ctx.font = '11px sans-serif';
        for (const n of labelled) ctx.fillText(n.name, n.x + radius(n) + 3, n.y + 3);
        ctx.restore();
    }

    // ---- Interaction ------------------------------------------------------
    function toGraph(x, y) { return { x: (x - panX) / scale, y: (y - panY) / scale }; }

    function nodeAt(x, y) {
        const p = toGraph(x, y);
        for (let i = nodes.length - 1; i >= 0; i--) {
            const n = nodes[i];
            const dx = p.x - n.x, dy = p.y - n.y;
            if (dx * dx + dy * dy <= Math.pow(radius(n) + 3, 2)) return n;
        }
        return null;
    }

    canvas.addEventListener('mousedown', e => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        dragNode = nodeAt(x, y);
        if (!dragNode) panning = true;
        lastX = x; lastY = y;
    });
    canvas.addEventListener('mousemove', e => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        if (dragNode) {
            const p = toGraph(x, y);
            dragNode.x = p.x; dragNode.y = p.y;
            kick(); draw();
        } else if (panning) {
            panX += x - lastX; panY += y - lastY;
            lastX = x; lastY = y; draw();
        }
    });
    window.addEventListener('mouseup', e => {
        if (dragNode) {
            const rect = canvas.getBoundingClientRect();
            const moved = Math.abs(e.clientX - rect.left - lastX) +
                          Math.abs(e.clientY - rect.top - lastY);
            if (moved < 4) inspect(dragNode);
        }
        dragNode = null; panning = false;
    });
    canvas.addEventListener('wheel', e => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.1 : 0.9;
        panX = x - (x - panX) * factor;
        panY = y - (y - panY) * factor;
        scale *= factor;
        draw();
    }, { passive: false });

    function inspect(node) {
        selected = node;
        const panel = document.getElementById('kg-inspector');
        panel.classList.remove('d-none');
        const rels = edges
            .filter(e => e.source_node_id === node.id || e.target_node_id === node.id)
            .map(e => {
                const otherId = e.source_node_id === node.id ? e.target_node_id : e.source_node_id;
                const other = nodeById[otherId];
                if (!other) return '';
                const arrow = e.source_node_id === node.id ? '→' : '←';
                return `<div><a href="#" data-node="${otherId}" class="kg-nav">` +
                       `${esc(node.name)} ${arrow}${esc(e.relation)}${arrow === '→' ? '→' : ''} ${esc(other.name)}</a></div>`;
            }).join('');
        panel.innerHTML = `
            <div style="font-weight: 600;">${esc(node.name)}</div>
            <div style="color: var(--app-text-muted);">${esc(node.entity_type)} · weight ${node.weight}</div>
            <p class="mt-2">${esc(node.description || '')}</p>
            <div style="font-weight: 600;" class="mt-2">Relationships</div>
            ${rels || '<div style="color: var(--app-text-muted);">None in view</div>'}`;
        panel.querySelectorAll('.kg-nav').forEach(a => a.addEventListener('click', ev => {
            ev.preventDefault();
            const target = nodeById[parseInt(a.dataset.node)];
            if (target) { centreOn(target); inspect(target); }
        }));
        draw();
    }

    function centreOn(node) {
        panX = canvas.clientWidth / 2 - node.x * scale;
        panY = canvas.clientHeight / 2 - node.y * scale;
        draw();
    }

    window.searchEntity = async function () {
        const q = document.getElementById('kg-search').value.trim();
        if (!q) return;
        const scope = document.getElementById('kg-scope').value;
        const r = await fetch(`/knowledge/api/search?q=${encodeURIComponent(q)}&scope=${encodeURIComponent(scope)}`)
            .then(x => x.json());
        if (r.results && r.results.length) {
            const hit = nodeById[r.results[0].id];
            if (hit) { centreOn(hit); inspect(hit); return; }
        }
        AppToast.warning('Not found', 'No matching entity in this scope.');
    };

    // ---- Data + management ------------------------------------------------
    function esc(text) {
        const div = document.createElement('div');
        div.textContent = text ?? '';
        return div.innerHTML;
    }

    window.loadGraph = async function () {
        const scope = document.getElementById('kg-scope').value;
        const g = await fetch(`/knowledge/api/graph?scope=${encodeURIComponent(scope)}`)
            .then(r => r.json());
        const w = canvas.clientWidth, h = canvas.clientHeight;
        nodes = g.nodes.map(n => ({
            ...n,
            x: w / 2 + (Math.sin(n.id * 7.13) * w) / 3,
            y: h / 2 + (Math.cos(n.id * 3.77) * h) / 3,
            vx: 0, vy: 0,
        }));
        edges = g.edges;
        nodeById = Object.fromEntries(nodes.map(n => [n.id, n]));
        selected = null;
        document.getElementById('kg-inspector').classList.add('d-none');
        document.getElementById('kg-cap-note').textContent =
            g.capped ? `Showing top ${g.nodes.length} of ${g.total_nodes} entities` : '';
        scale = 1; panX = 0; panY = 0;
        kick();
    };

    async function refreshStatus() {
        const status = await fetch('/knowledge/api/status').then(r => r.json());
        const global = status.scopes.global;
        document.getElementById('kg-global-stats').textContent =
            `${global.nodes} entities · ${global.edges} relationships · ` +
            `${global.plan.pending_chunks} chunk(s) pending`;
        document.getElementById('kg-global-cancel').classList.toggle('d-none', !global.building);
        const progress = document.getElementById('kg-global-progress');
        progress.classList.toggle('d-none', !global.building && !global.last_result);
        if (global.building) {
            progress.textContent = 'Building...';
            setTimeout(refreshStatus, 2000);
        } else if (global.last_result) {
            const r = global.last_result;
            progress.textContent = `Last build: ${r.status} — ${r.chunks || 0} chunks, ` +
                `${r.entities || 0} entities, ${r.relationships || 0} relationships` +
                (r.skipped_chunks ? `, ${r.skipped_chunks} skipped` : '') +
                (r.error ? ` — ${r.error}` : '');
        }

        const list = document.getElementById('kg-conversation-list');
        const scopeSelect = document.getElementById('kg-scope');
        const current = scopeSelect.value;
        scopeSelect.innerHTML = '<option value="global">Global</option>' +
            (status.conversation_graphs || []).map(g =>
                `<option value="conv:${g.conversation_id}">${esc(g.name)}</option>`).join('');
        scopeSelect.value = [...scopeSelect.options].some(o => o.value === current)
            ? current : 'global';
        list.innerHTML = (status.conversation_graphs || []).length
            ? status.conversation_graphs.map(g => `
                <div class="d-flex align-items-center justify-content-between py-1">
                    <span>${esc(g.name)} <span style="color: var(--app-text-muted);">
                        ${g.nodes} entities · ${g.edges} relationships${g.building ? ' · building...' : ''}</span></span>
                    <span>
                        <button class="btn btn-app-ghost btn-sm" onclick="startBuild('conv:${g.conversation_id}')">
                            <i class="bi bi-hammer"></i></button>
                        <button class="btn btn-app-ghost btn-sm" style="color: var(--app-danger);"
                                onclick="clearScope('conv:${g.conversation_id}')"><i class="bi bi-trash"></i></button>
                    </span>
                </div>`).join('')
            : '<span style="color: var(--app-text-muted);">No conversation graphs enabled yet. ' +
              'Enable one in a conversation\'s settings.</span>';
    }

    window.startBuild = async function (scope) {
        const status = await fetch('/knowledge/api/status').then(r => r.json());
        const pending = scope === 'global'
            ? status.scopes.global.plan.pending_chunks
            : ((status.conversation_graphs || []).find(g => 'conv:' + g.conversation_id === scope)
                ?.plan.pending_chunks ?? 0);
        if (!confirm(`Build ${scope === 'global' ? 'the global graph' : 'this conversation graph'}?\n` +
                     `${pending} chunk(s) will be extracted (one model call each).`)) return;
        const r = await fetch('/knowledge/api/build', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope }),
        });
        if (r.status === 409) { AppToast.warning('Busy', 'A build is already running for this scope.'); }
        refreshStatus();
    };

    window.cancelBuild = async function (scope) {
        await fetch('/knowledge/api/cancel', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope }),
        });
        refreshStatus();
    };

    window.clearScope = async function (scope) {
        if (!confirm('Clear this graph? All its entities and relationships will be removed.')) return;
        const r = await fetch('/knowledge/api/clear', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope }),
        });
        if (r.status === 409) { AppToast.warning('Busy', 'Cannot clear while a build is running.'); return; }
        refreshStatus(); loadGraph();
    };

    document.getElementById('kg-legend').innerHTML = Object.entries(TYPE_COLOURS)
        .map(([type, colour]) =>
            `<span class="me-2"><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${colour};"></span> ${type}</span>`)
        .join('');

    fitCanvas();
    refreshStatus();
    loadGraph();
})();
