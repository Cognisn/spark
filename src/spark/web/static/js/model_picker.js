/**
 * Spark: the model picker (M11).
 *
 * Models are grouped by connection (its display name), each with its credit tier.
 * On a metered connection the group also shows the user's credits left this month
 * (from /api/usage/me), or that the allowance is used. Personal mode and unmetered
 * connections show no credits.
 *
 *   SparkModelPicker.load()                      models, default, mandatory, standings
 *   SparkModelPicker.renderList(el, data, opts)  the chat page's list (opts.current, opts.onSelect)
 *   SparkModelPicker.optionsHtml(data, selected) <optgroup>s for a <select> (escaped)
 *
 * Server text is only ever set as text, or escaped before it reaches HTML.
 */
(function () {
    const TIERS = { lower: 'Lower tier', standard: 'Standard', higher: 'Higher tier' };

    function escape(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function amount(n) {
        const value = Number(n);
        return value.toLocaleString('en-AU', {
            minimumFractionDigits: Number.isInteger(value) ? 0 : 2,
            maximumFractionDigits: 2,
        });
    }

    async function load() {
        const [models, usage] = await Promise.all([
            fetch('/conversations/api/models').then(r => r.json()),
            fetch('/api/usage/me', { headers: { Accept: 'application/json' } })
                .then(r => (r.ok ? r.json() : null))
                .catch(() => null),
        ]);
        return {
            models: (models && (models.models || models)) || [],
            default_model: (models && models.default_model) || null,
            mandatory: Boolean(models && models.mandatory),
            standings: (usage && usage.connections) || {},
        };
    }

    function groups(models) {
        const order = [];
        const byKey = {};
        (models || []).forEach(m => {
            const key = m.connection_id || m.provider || 'other';
            if (!byKey[key]) {
                byKey[key] = {
                    connection_id: m.connection_id || null,
                    name: m.connection_name || m.provider || 'Other',
                    models: [],
                };
                order.push(key);
            }
            byKey[key].models.push(m);
        });
        return order.map(k => byKey[k]);
    }

    function tierLabel(tier) {
        return TIERS[tier] || '';
    }

    function creditsLabel(standing) {
        if (!standing || standing.state === 'unlimited' || standing.remaining == null) return '';
        if (standing.state === 'exceeded') return 'Allowance used this month';
        return `${amount(standing.remaining)} credits left this month`;
    }

    function standingOf(data, group) {
        return group.connection_id ? (data.standings || {})[group.connection_id] : null;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function renderList(container, data, opts) {
        const options = opts || {};
        container.replaceChildren();
        groups(data.models).forEach(group => {
            const standing = standingOf(data, group);
            const head = el('div', 'model-picker-group d-flex align-items-baseline gap-2');
            head.style.cssText = 'font-size:0.6875rem; font-weight:600; color:var(--app-text-muted);'
                + ' text-transform:uppercase; letter-spacing:0.04em; padding:0.5rem 0.5rem 0.25rem;'
                + ' margin-top:0.25rem;';
            head.appendChild(el('span', null, group.name));
            const left = creditsLabel(standing);
            if (left) {
                const badge = el('span', 'model-picker-credits', left);
                badge.style.cssText = 'text-transform:none; letter-spacing:0; font-weight:500; color:'
                    + (standing.state === 'exceeded' ? 'var(--app-danger)'
                        : standing.state === 'warning' ? 'var(--app-warning)' : 'var(--app-text-secondary)')
                    + ';';
                if (standing.connection_id) badge.dataset.connection = standing.connection_id;
                head.appendChild(badge);
            }
            container.appendChild(head);

            group.models.forEach(m => {
                const active = m.id === options.current;
                const item = el('div', `d-flex align-items-center gap-2 px-2 py-2 rounded model-selector-item${active ? ' active' : ''}`);
                item.style.cssText = 'cursor:pointer; font-size:0.8125rem;' + (active ? ' background:var(--app-active-bg);' : '');
                item.tabIndex = 0;
                item.setAttribute('role', 'button');
                item.dataset.modelName = String(m.name || m.id).toLowerCase();
                item.dataset.modelId = String(m.id).toLowerCase();
                item.dataset.tier = m.tier || '';
                const icon = el('i', active ? 'bi bi-check-circle-fill' : 'bi bi-circle');
                icon.style.cssText = active ? 'color:var(--app-accent); flex-shrink:0;'
                    : 'color:var(--app-text-muted); opacity:0.3; flex-shrink:0;';
                item.appendChild(icon);
                const body = el('div');
                body.style.cssText = 'flex:1; min-width:0;';
                const title = el('div', null, m.name || m.id);
                title.style.cssText = `font-weight:${active ? 600 : 400}; color:var(--app-text-primary);`
                    + ' white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
                body.appendChild(title);
                const sub = el('div', null, m.id);
                sub.style.cssText = 'font-size:0.6875rem; color:var(--app-text-muted);';
                body.appendChild(sub);
                item.appendChild(body);
                const side = el('div', 'd-flex align-items-center gap-2');
                side.style.flexShrink = '0';
                if (m.tier) {
                    const tier = el('span', `badge-app model-tier model-tier-${m.tier}`, tierLabel(m.tier));
                    tier.style.fontSize = '0.625rem';
                    side.appendChild(tier);
                }
                if (m.supports_tools) {
                    const tools = el('span', null, 'tools');
                    tools.style.cssText = 'font-size:0.5625rem; color:var(--app-success);';
                    side.appendChild(tools);
                }
                const ctx = Number(m.context_length) || 0;
                if (ctx > 0) {
                    const size = ctx >= 1000000 ? (ctx / 1000000).toFixed(1) + 'M'
                        : ctx >= 1000 ? Math.round(ctx / 1000) + 'K' : String(ctx);
                    const span = el('span', null, size);
                    span.style.cssText = 'font-size:0.625rem; color:var(--app-text-muted);';
                    side.appendChild(span);
                }
                item.appendChild(side);
                if (options.onSelect) {
                    item.addEventListener('click', () => options.onSelect(m.id, m));
                    item.addEventListener('keydown', e => { if (e.key === 'Enter') options.onSelect(m.id, m); });
                }
                container.appendChild(item);
            });
        });
    }

    function optionsHtml(data, selected) {
        const list = groups(data.models);
        if (!list.length) {
            return '<option value="">No models available. Ask an administrator for a connection.</option>';
        }
        return list.map(group => {
            const left = creditsLabel(standingOf(data, group));
            const label = left ? `${group.name} (${left})` : group.name;
            const options = group.models.map(m => {
                const tier = tierLabel(m.tier);
                const text = tier && m.tier !== 'standard' ? `${m.name || m.id} (${tier})` : (m.name || m.id);
                return `<option value="${escape(m.id)}" data-tier="${escape(m.tier || '')}"`
                    + `${m.id === selected ? ' selected' : ''}>${escape(text)}</option>`;
            }).join('');
            return `<optgroup label="${escape(label)}">${options}</optgroup>`;
        }).join('');
    }

    window.SparkModelPicker = { load, groups, tierLabel, creditsLabel, renderList, optionsHtml };
})();
