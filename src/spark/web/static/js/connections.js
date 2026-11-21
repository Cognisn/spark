/**
 * Spark: the connections screens (M3 to M10).
 *
 * Renders into <div id="connections-app" data-server="true|false">: the list of
 * connections, the add and edit modal (one per service, its fields from the
 * server), the models view, and in server mode the metering and assignment
 * dialogs. Every request goes to /api/connections; csrf.js adds the CSRF token.
 * Secrets are write-only: the page only ever learns whether each one is set.
 * Text from the server is always placed with textContent, never as HTML.
 */
(function () {
    'use strict';

    const API = '/api/connections';
    const TIER_LABELS = { lower: 'Lower', standard: 'Standard', higher: 'Higher' };
    const SCOPE_LABELS = {
        local: 'Local', in_region: 'In Region', cross_region: 'Cross Region', global: 'Global',
    };

    let root = null;
    let server = false;
    let services = [];
    let scopes = [];
    let tiers = [];

    // -- small helpers ------------------------------------------------------------

    function h(tag, attrs, ...children) {
        const el = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs || {})) {
            if (value === null || value === undefined || value === false) continue;
            if (key === 'class') el.className = value;
            else if (key === 'text') el.textContent = value;
            else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
            else if (value === true) el.setAttribute(key, '');
            else el.setAttribute(key, String(value));
        }
        for (const child of children.flat()) {
            if (child === null || child === undefined || child === false) continue;
            el.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        }
        return el;
    }

    function icon(name) { return h('i', { class: 'bi ' + name }); }

    async function call(method, path, body) {
        const options = { method, headers: { Accept: 'application/json' } };
        if (body !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        const resp = await fetch(API + path, options);
        let data = {};
        try { data = await resp.json(); } catch (e) { data = {}; }
        if (!resp.ok) {
            const error = new Error(data.error || data.detail || ('Request failed (' + resp.status + ')'));
            error.data = data;
            error.status = resp.status;
            throw error;
        }
        return data;
    }

    function toast(kind, title, message) {
        if (window.AppToast) window.AppToast[kind](title, message);
    }

    function when(rule, values) {
        return Object.entries(rule || {}).every(([path, allowed]) => allowed.includes(values[path]));
    }

    function since(iso) {
        if (!iso) return '';
        const at = new Date(iso);
        return isNaN(at) ? iso : at.toLocaleString();
    }

    /** A Bootstrap modal built on the fly, removed when hidden. */
    function dialog(title, body, footer, size) {
        const modal = h('div', { class: 'modal fade conn-modal', tabindex: '-1' },
            h('div', { class: 'modal-dialog modal-dialog-scrollable ' + (size || '') },
                h('div', { class: 'modal-content' },
                    h('div', { class: 'modal-header' },
                        h('h5', { class: 'modal-title', text: title }),
                        h('button', { type: 'button', class: 'btn-close', 'data-bs-dismiss': 'modal', 'aria-label': 'Close' })),
                    h('div', { class: 'modal-body' }, body),
                    footer ? h('div', { class: 'modal-footer' }, footer) : null)));
        document.body.appendChild(modal);
        const instance = new bootstrap.Modal(modal);
        modal.addEventListener('hidden.bs.modal', () => modal.remove());
        instance.show();
        return { modal, close: () => instance.hide() };
    }

    function alertBox() {
        return h('div', { class: 'alert-app alert-app-danger mb-3 conn-error', role: 'alert', hidden: true });
    }

    function showError(box, message) {
        box.textContent = message;
        box.hidden = false;
    }

    // -- the list -----------------------------------------------------------------

    async function load() {
        const list = root.querySelector('.conn-list');
        try {
            const data = await call('GET', '');
            renderList(list, data.connections);
        } catch (err) {
            list.replaceChildren(h('div', { class: 'alert-app alert-app-warning', text: err.message }));
        }
    }

    function statusBadge(item) {
        const labels = { running: ['Running', 'badge-app-success'], disabled: ['Disabled', 'badge-app-secondary'], failed: ['Not started', 'badge-app-danger'] };
        const [label, cls] = labels[item.status] || [item.status, 'badge-app-secondary'];
        const badge = h('span', { class: 'badge-app ' + cls, text: label });
        if (item.failure) badge.title = item.failure;
        return badge;
    }

    function renderList(list, items) {
        if (!items.length) {
            list.replaceChildren(h('p', { class: 'conn-empty', text: 'No connections yet. Add one to offer its models.' }));
            return;
        }
        const head = ['Name', 'Service', 'Region', 'Residency'];
        if (server) head.push('Metered');
        head.push('Models');
        if (server) head.push('Assigned');
        head.push('Last discovery', 'Status');
        const rows = items.map((item) => {
            const cells = [
                h('td', {}, h('div', { class: 'conn-name', text: item.name }), h('div', { class: 'conn-id', text: item.id }),
                    h('div', { class: 'conn-actions' }, actionsFor(item))),
                h('td', { text: item.service_label }),
                h('td', { text: item.region || '' }),
                h('td', {}, h('div', { text: item.residency_text }),
                    item.region_choice ? h('div', { class: 'conn-muted', text: 'Region choice: ' + choiceText(item.region_choice) }) : null),
            ];
            if (server) cells.push(h('td', { text: item.metered ? 'Metered' : 'Not metered' }));
            cells.push(h('td', { text: String(item.enabled_models) }));
            if (server) cells.push(h('td', { text: item.assignments ? String(item.assignments) : 'Administrators only' }));
            const disc = item.discovery;
            cells.push(h('td', { class: 'conn-discovery' },
                disc ? h('div', { text: disc.summary }) : h('div', { class: 'conn-muted', text: 'Not run' }),
                disc ? h('div', { class: 'conn-muted', text: since(disc.ran_at) }) : null));
            cells.push(h('td', {}, statusBadge(item)));
            return h('tr', { 'data-connection': item.id }, cells);
        });
        list.replaceChildren(h('div', { class: 'app-table-responsive' },
            h('table', { class: 'app-table conn-table' },
                h('thead', {}, h('tr', {}, head.map((t) => h('th', { text: t })))),
                h('tbody', {}, rows))));
    }

    function actionsFor(item) {
        const button = (label, iconName, handler, extra) => h('button', {
            type: 'button', class: 'btn btn-app-ghost btn-sm', title: label, 'aria-label': label + ' ' + item.name,
            onclick: handler, ...(extra || {}),
        }, icon(iconName));
        return h('div', { class: 'd-flex flex-wrap gap-1' },
            button('Edit', 'bi-pencil', () => openModal(null, item.id)),
            button('Models', 'bi-list-check', () => openModels(item)),
            item.regions_mode ? button('Regions', 'bi-globe-asia-australia', () => openRegions(item)) : null,
            server ? button('Metering', 'bi-speedometer2', () => openMetering(item)) : null,
            server ? button('Assignments', 'bi-people', () => openAssignments(item)) : null,
            button(item.enabled ? 'Disable' : 'Enable', item.enabled ? 'bi-pause-circle' : 'bi-play-circle', () => toggle(item)),
            button('Rename', 'bi-input-cursor-text', () => rename(item)),
            button('Delete', 'bi-trash', () => remove(item)));
    }

    async function toggle(item) {
        try {
            await call('POST', '/' + encodeURIComponent(item.id) + '/enabled', { enabled: !item.enabled });
            await load();
        } catch (err) { toast('danger', 'Not changed', err.message); }
    }

    async function rename(item) {
        const name = window.prompt('A new name for ' + item.name, item.name);
        if (name === null || !name.trim() || name.trim() === item.name) return;
        try {
            await call('POST', '/' + encodeURIComponent(item.id) + '/rename', { name: name.trim() });
            await load();
        } catch (err) { toast('danger', 'Not renamed', err.message); }
    }

    async function remove(item) {
        const sure = window.confirm(
            'Delete the connection ' + item.name + '? Its models, metering, assignments and stored ' +
            'credentials are removed too. This cannot be undone.');
        if (!sure) return;
        try {
            await call('DELETE', '/' + encodeURIComponent(item.id));
            toast('success', 'Connection deleted', item.name);
            await load();
        } catch (err) {
            if (err.status === 409 && err.data && err.data.actions) {
                const body = h('div', {},
                    h('p', { text: 'Scheduled actions use ' + item.name + '. Change their model or delete them first:' }),
                    h('ul', {}, err.data.actions.map((a) => h('li', { text: a.name + (a.enabled ? '' : ' (disabled)') }))));
                dialog('Connection in use', body, h('button', { class: 'btn btn-app-ghost', 'data-bs-dismiss': 'modal', text: 'Close' }));
            } else {
                toast('danger', 'Not deleted', err.message);
            }
        }
    }

    // -- the add and edit modal ---------------------------------------------------

    function fieldInput(field, value, secretSet) {
        const id = 'conn-f-' + field.path.replace(/[^a-z0-9_]/gi, '-');
        let input;
        if (field.type === 'bool') {
            input = h('input', { type: 'checkbox', class: 'form-check-input', id, role: 'switch' });
            input.checked = Boolean(value);
            return { input, row: h('div', { class: 'form-check form-switch mb-3', 'data-path': field.path },
                input, h('label', { class: 'form-check-label', for: id, text: field.label }),
                field.help ? h('div', { class: 'form-text', text: field.help }) : null) };
        }
        if (field.type === 'select') {
            input = h('select', { class: 'form-select form-select-sm', id },
                field.options.map((o) => h('option', { value: o.value, text: o.label, 'data-when': JSON.stringify(o.when || {}) })));
            input.value = value || field.default || field.options[0].value;
        } else if (field.type === 'secret') {
            input = h('input', {
                type: 'password', class: 'form-control form-control-sm', id, autocomplete: 'new-password',
                placeholder: secretSet ? 'Set (type to replace)' : 'Not set',
            });
        } else {
            input = h('input', {
                type: field.type === 'number' ? 'number' : 'text', class: 'form-control form-control-sm', id,
                placeholder: field.placeholder || (field.default != null ? String(field.default) : ''),
            });
            if (value !== undefined && value !== null) input.value = value;
        }
        const label = h('label', { class: 'form-label', for: id }, field.label,
            field.required ? h('span', { class: 'conn-required', text: ' *' }) : null,
            field.type === 'secret' ? h('span', {
                class: 'badge-app ms-2 ' + (secretSet ? 'badge-app-success' : 'badge-app-secondary'),
                text: secretSet ? 'Set' : 'Not set',
            }) : null);
        return { input, row: h('div', { class: 'mb-3', 'data-path': field.path }, label, input,
            field.help ? h('div', { class: 'form-text', text: field.help }) : null) };
    }

    async function openModal(serviceKey, connectionId) {
        let existing = null;
        if (connectionId) {
            try { existing = await call('GET', '/' + encodeURIComponent(connectionId)); }
            catch (err) { toast('danger', 'Could not open', err.message); return; }
            serviceKey = existing.service;
        }
        const service = services.find((s) => s.key === serviceKey);
        if (!service) { toast('warning', 'Not editable here', 'This connection is not one of the services listed.'); return; }
        const values = existing ? existing.values : {};
        const secretsSet = existing ? existing.secrets : {};
        const error = alertBox();
        const name = h('input', { type: 'text', class: 'form-control form-control-sm', id: 'conn-name', maxlength: '128' });
        name.value = existing ? existing.name : service.label;
        const inputs = {};
        const rows = service.fields.filter((f) => f.type !== 'fixed').map((field) => {
            const built = fieldInput(field, values[field.path], secretsSet[field.path]);
            inputs[field.path] = { field, input: built.input, row: built.row };
            return built.row;
        });

        const residency = existing ? existing.residency : null;
        const useDefault = h('input', { type: 'checkbox', class: 'form-check-input', id: 'conn-res-default' });
        useDefault.checked = !existing || JSON.stringify(existing.residency) === JSON.stringify(existing.default_residency);
        const scopeBoxes = scopes.map((scope) => {
            const box = h('input', { type: 'checkbox', class: 'form-check-input', id: 'conn-scope-' + scope, value: scope });
            box.checked = residency ? residency.scopes.includes(scope) : false;
            return h('div', { class: 'form-check form-check-inline' }, box,
                h('label', { class: 'form-check-label', for: 'conn-scope-' + scope, text: SCOPE_LABELS[scope] || scope }));
        });
        const regions = h('input', { type: 'text', class: 'form-control form-control-sm', id: 'conn-res-regions', placeholder: 'Any Region' });
        regions.value = residency && residency.regions ? residency.regions.join(', ') : '';
        const custom = h('div', { class: 'conn-residency-custom' },
            h('div', { class: 'mb-2' }, scopeBoxes),
            h('label', { class: 'form-label', for: 'conn-res-regions', text: 'Regions requests may be processed in (comma separated)' }),
            regions);
        const defaults = { in_region: 'local and in-Region processing in the connection\'s own Region', global: 'global processing', local: 'local processing only' };

        function current() {
            const v = {};
            for (const [path, item] of Object.entries(inputs)) {
                if (item.field.type === 'bool') v[path] = item.input.checked;
                else if (item.field.type !== 'secret') v[path] = item.input.value;
            }
            return v;
        }

        function refresh() {
            const v = current();
            for (const item of Object.values(inputs)) {
                item.row.hidden = !when(item.field.when, v);
                if (item.field.type === 'select') {
                    for (const option of item.input.options) {
                        option.hidden = !when(JSON.parse(option.dataset.when || '{}'), v);
                    }
                    if (item.input.selectedOptions[0] && item.input.selectedOptions[0].hidden) {
                        item.input.value = item.field.default;
                        return refresh();
                    }
                }
            }
            custom.hidden = useDefault.checked;
        }
        for (const item of Object.values(inputs)) item.input.addEventListener('change', refresh);
        useDefault.addEventListener('change', refresh);

        const body = h('form', { class: 'conn-form', onsubmit: (e) => e.preventDefault() },
            error,
            h('div', { class: 'mb-3' }, h('label', { class: 'form-label', for: 'conn-name', text: 'Name' }), name),
            rows,
            h('fieldset', { class: 'conn-residency' },
                h('legend', { text: 'Residency' }),
                h('div', { class: 'form-check mb-2' }, useDefault,
                    h('label', { class: 'form-check-label', for: 'conn-res-default', text: 'Use this service\'s default: ' + defaults[service.residency] })),
                custom),
            h('p', { class: 'conn-muted mb-0', text: 'Credentials are kept in the secrets store. They are never shown again, only whether each is set.' }));
        const save = h('button', { type: 'button', class: 'btn btn-app-primary', text: existing ? 'Save' : 'Add connection' });
        const view = dialog((existing ? 'Edit ' : 'Add ') + service.label + ' connection', body,
            [h('button', { type: 'button', class: 'btn btn-app-ghost', 'data-bs-dismiss': 'modal', text: 'Cancel' }), save], 'modal-lg');
        refresh();

        save.addEventListener('click', async () => {
            error.hidden = true;
            const v = current();
            const typed = {};
            for (const [path, item] of Object.entries(inputs)) {
                if (item.field.type === 'secret' && !item.row.hidden && item.input.value) typed[path] = item.input.value;
            }
            const payload = {
                name: name.value.trim(),
                values: v,
                secrets: typed,
                residency: useDefault.checked ? { default: true } : {
                    default: false,
                    scopes: scopes.filter((s) => body.querySelector('#conn-scope-' + s).checked),
                    regions: regions.value,
                },
            };
            if (!existing) payload.service = service.key;
            save.disabled = true;
            try {
                const saved = existing
                    ? await call('PUT', '/' + encodeURIComponent(existing.id), payload)
                    : await call('POST', '', payload);
                for (const item of Object.values(inputs)) if (item.field.type === 'secret') item.input.value = '';
                view.close();
                toast('success', 'Connection saved', 'Checking which models it can reach.');
                await load();
                await discover(saved.id, true);
            } catch (err) {
                showError(error, err.message);
            } finally {
                save.disabled = false;
            }
        });
    }

    async function discover(connectionId, quiet) {
        try {
            const view = await call('POST', '/' + encodeURIComponent(connectionId) + '/discover');
            await load();
            if (!quiet) return view;
            const d = view.discovery;
            if (d && !d.ok) toast('warning', 'Discovery', d.summary);
            return view;
        } catch (err) {
            toast('danger', 'Discovery failed', err.message);
            return null;
        }
    }

    // -- the models view ----------------------------------------------------------

    async function openModels(item) {
        const holder = h('div', {}, h('div', { class: 'app-spinner mx-auto' }));
        const run = h('button', { type: 'button', class: 'btn btn-app-outline btn-sm' }, icon('bi-search'), ' Check reachable models');
        const view = dialog('Models on ' + item.name, holder, [run,
            h('button', { type: 'button', class: 'btn btn-app-ghost', 'data-bs-dismiss': 'modal', text: 'Close' })], 'modal-xl');
        const path = '/' + encodeURIComponent(item.id);
        run.addEventListener('click', async () => {
            run.disabled = true;
            const data = await discover(item.id, false);
            run.disabled = false;
            if (data) renderModels(holder, item, data);
        });
        try { renderModels(holder, item, await call('GET', path + '/models')); }
        catch (err) { holder.replaceChildren(h('div', { class: 'alert-app alert-app-danger', text: err.message })); }
        return view;
    }

    function reachBadge(reachable) {
        if (reachable === true) return h('span', { class: 'badge-app badge-app-success', text: 'Reachable' });
        if (reachable === false) return h('span', { class: 'badge-app badge-app-warning', text: 'Not reachable' });
        return h('span', { class: 'badge-app badge-app-secondary', text: 'Not checked' });
    }

    function renderModels(holder, item, data) {
        const path = '/' + encodeURIComponent(item.id);
        const d = data.discovery;
        const status = h('p', { class: 'conn-muted' },
            d ? ('Last discovery ' + since(d.ran_at) + ': ' + d.summary) : 'Discovery has not run on this connection yet.');
        const note = data.curated ? null : h('p', { class: 'conn-muted', text: 'Every catalogue model is offered until you change one; your first change keeps the others as they are.' });
        const rows = data.models.map((m) => {
            const toggle = h('input', { type: 'checkbox', class: 'form-check-input', role: 'switch', 'aria-label': 'Enable ' + m.model_id });
            toggle.checked = m.enabled && !m.refused;
            toggle.disabled = Boolean(m.refused);
            const tier = h('select', { class: 'form-select form-select-sm conn-tier', 'aria-label': 'Tier of ' + m.model_id },
                tiers.map((t) => h('option', { value: t, text: TIER_LABELS[t] || t })));
            tier.value = m.tier;
            const change = async (body) => {
                try { renderModels(holder, item, await call('PUT', path + '/models', { model_id: m.model_id, ...body })); load(); }
                catch (err) { toast('danger', 'Not changed', err.message); }
            };
            toggle.addEventListener('change', () => change({ enabled: toggle.checked }));
            tier.addEventListener('change', () => change({ tier: tier.value }));
            const caps = ['tools', 'vision', 'prompt_caching', 'structured_output'].filter((c) => m[c]).map((c) => c.replace('_', ' '));
            return h('tr', { 'data-model': m.model_id, class: m.refused ? 'conn-refused' : '' },
                h('td', {}, h('div', { class: 'conn-model', text: m.model_id }),
                    m.overlay ? h('span', { class: 'badge-app badge-app-primary', text: 'Declared in SPARK' }) : null),
                h('td', {}, reachBadge(m.reachable)),
                h('td', { class: 'conn-muted', text: m.context_window.toLocaleString() + ' / ' + (m.output_limit ? m.output_limit.toLocaleString() : '') }),
                h('td', { class: 'conn-muted', text: caps.join(', ') }),
                h('td', {}, m.refused ? h('div', { class: 'conn-refusal', text: 'Refused: ' + m.refused })
                    : h('span', { class: 'conn-muted', text: processedText(m.residency_scope, m.regions) })),
                h('td', {}, h('div', { class: 'form-check form-switch' }, toggle)),
                h('td', {}, tier));
        });
        const table = rows.length ? h('div', { class: 'app-table-responsive' }, h('table', { class: 'app-table conn-table' },
            h('thead', {}, h('tr', {}, ['Model', 'Discovery', 'Context / output', 'Capabilities', 'Residency', 'Enabled', 'Tier'].map((t) => h('th', { text: t })))),
            h('tbody', {}, rows))) : h('p', { class: 'conn-muted', text: 'The catalogue declares no models for this service in its Region.' });
        const undeclared = data.undeclared.length ? h('div', { class: 'mt-4' },
            h('h6', { text: 'Reachable but not in the catalogue' }),
            h('p', { class: 'conn-muted', text: 'Declare a model to add it to SPARK\'s catalogue for this service. Check its limits with the service first.' }),
            data.undeclared.map((u) => declareRow(holder, item, u))) : null;
        holder.replaceChildren(...[status, note, table, discoveredTable(data), driftList(data), undeclared, declareTyped(holder, item, data)].filter(Boolean));
    }

    // -- discovered models, drift and Region choice ---------------------------------

    function processedText(scope, regions) {
        const label = SCOPE_LABELS[scope] || scope;
        return regions && regions.length ? label + ': ' + regions.join(', ') : label;
    }

    function choiceText(choice) {
        if (choice.geography) return choice.geography;
        if (choice.deployment) return choice.deployment.type + ' in ' + choice.deployment.azure_region;
        const regions = (choice.regions || []).join(', ');
        return regions + (choice.allow_global ? ' (and global)' : '');
    }

    // Every model the service lists: where it is processed, whether conduit allows it under
    // the connection's residency, and conduit's reason when it does not. Nothing is hidden.
    function discoveredTable(data) {
        if (!data.discovered || !data.discovered.length) return null;
        const rows = data.discovered.map((m) => h('tr', { 'data-discovered': m.model_id, class: m.allowed ? '' : 'conn-refused' },
            h('td', {}, h('div', { class: 'conn-model', text: m.model_id }), m.label ? h('div', { class: 'conn-id', text: m.label }) : null),
            h('td', { class: 'conn-muted', text: processedText(m.scope, m.regions) }),
            h('td', {}, m.allowed ? h('span', { class: 'badge-app badge-app-success', text: 'Usable' })
                : h('div', { class: 'conn-refusal', text: 'Refused: ' + m.reason })),
            h('td', { class: 'conn-muted', text: (m.drift || []).map((r) => DRIFT_LABELS[r] || r).join('; ') })));
        return h('div', { class: 'mt-4' },
            h('h6', { text: 'Models the service lists' }),
            h('p', { class: 'conn-muted', text: 'Where the service says each model is processed, and whether it is usable under this connection\'s residency and Region choice.' }),
            h('div', { class: 'app-table-responsive' }, h('table', { class: 'app-table conn-table' },
                h('thead', {}, h('tr', {}, ['Model', 'Processed in', 'Usable', 'Notes'].map((t) => h('th', { text: t })))),
                h('tbody', {}, rows))));
    }

    const DRIFT_LABELS = {
        uncatalogued: 'No capability record; using defaults',
        unlisted: 'Not listed by the service',
        limits_differ: 'Limits differ from the catalogue',
        capabilities_differ: 'Capabilities differ from the catalogue',
        regions_declared: 'Regions declared here differ from the listing',
    };

    // Where the catalogue and the service's listing differ, as information only: no model is
    // hidden or refused for it.
    function driftList(data) {
        if (!data.drift || !data.drift.length) return null;
        return h('details', { class: 'mt-4 conn-drift' },
            h('summary', { text: 'Differences from the catalogue (' + data.drift.length + ')' }),
            h('p', { class: 'conn-muted mt-2', text: 'For information. conduit still allows every model its residency rules allow.' }),
            h('ul', { class: 'conn-rules' }, data.drift.map((d) => h('li', { 'data-drift': d.reason },
                h('span', { class: 'conn-model', text: d.model_id }), ': ' + d.text + (d.detail ? ' (' + d.detail + ')' : '')))));
    }

    async function openRegions(item) {
        const holder = h('div', {}, h('div', { class: 'app-spinner mx-auto' }));
        const error = alertBox();
        const save = h('button', { type: 'button', class: 'btn btn-app-primary', text: 'Save' });
        const view = dialog('Regions for ' + item.name, [error, holder], [save,
            h('button', { type: 'button', class: 'btn btn-app-ghost', 'data-bs-dismiss': 'modal', text: 'Cancel' })]);
        const path = '/' + encodeURIComponent(item.id) + '/regions';
        let read = () => null;
        try {
            const data = await call('GET', path);
            read = renderRegions(holder, data);
        } catch (err) { holder.replaceChildren(h('div', { class: 'alert-app alert-app-danger', text: err.message })); save.disabled = true; }
        save.addEventListener('click', async () => {
            error.hidden = true;
            save.disabled = true;
            try {
                await call('PUT', path, { choice: read() });
                view.close();
                toast('success', 'Regions', 'Saved. The connection restarts with the new choice.');
                await load();
            } catch (err) { showError(error, err.message); }
            finally { save.disabled = false; }
        });
    }

    // The choices conduit offers for the connection's service, and how each is set. Returns
    // a function that reads the form as the choice to save (null clears it).
    function renderRegions(holder, data) {
        const chosen = data.choice || {};
        const regions = data.listings.flatMap((l) => l.regions.map((r) => ({ ...r, listing: l })));
        const sources = data.listings.map((l) => h('div', { class: 'conn-muted', text: 'Source: ' + l.documentation + ' (as of ' + l.as_of + ')' }));
        const intro = h('p', { class: 'conn-muted', text: 'Choose where this connection\'s requests may be processed. The choice only narrows what the deployment\'s residency policy allows; leave it empty for no choice. conduit checks it and says why when it refuses one.' });
        let read;
        let controls;
        if (data.mode === 'processing') {
            const boxes = regions.filter((r) => r.invocation).map((r) => {
                const box = h('input', { type: 'checkbox', class: 'form-check-input', value: r.code, id: 'rg-' + r.code });
                box.checked = (chosen.regions || []).includes(r.code);
                return { box, el: h('div', { class: 'form-check' }, box, h('label', { class: 'form-check-label', for: 'rg-' + r.code, text: r.name + ' (' + r.code + ')' })) };
            });
            const global = h('input', { type: 'checkbox', class: 'form-check-input', id: 'rg-global' });
            global.checked = Boolean(chosen.allow_global);
            const globalNote = data.listings[0] && data.listings[0].global_option;
            controls = [h('div', { class: 'conn-region-list' }, boxes.map((b) => b.el)),
                globalNote ? h('div', { class: 'form-check mt-3' }, global, h('label', { class: 'form-check-label', for: 'rg-global', text: 'Also allow global processing: ' + globalNote })) : null];
            read = () => {
                const picked = boxes.filter((b) => b.box.checked).map((b) => b.box.value);
                return picked.length ? { regions: picked, allow_global: global.checked } : null;
            };
        } else if (data.mode === 'inference_geo') {
            const select = h('select', { class: 'form-select', 'aria-label': 'Inference geography' },
                h('option', { value: '', text: 'No choice' }),
                h('option', { value: 'us', text: 'United States only' }),
                h('option', { value: 'global', text: 'Global (any geography)' }));
            select.value = chosen.geography || '';
            controls = [select];
            read = () => (select.value ? { geography: select.value } : null);
        } else if (data.mode === 'data_residency') {
            const select = h('select', { class: 'form-select', 'aria-label': 'Data residency Region' },
                h('option', { value: '', text: 'No choice' }),
                regions.filter((r) => r.invocation).map((r) => h('option', { value: r.code, text: r.name + (r.stores_only ? ' (stores data only; processing may happen anywhere)' : '') })));
            select.value = (chosen.regions || [])[0] || '';
            controls = [select];
            read = () => (select.value ? { regions: [select.value] } : null);
        } else {
            const dep = chosen.deployment || {};
            const type = h('select', { class: 'form-select', 'aria-label': 'Deployment type' },
                h('option', { value: '', text: 'No choice' }),
                h('option', { value: 'global', text: 'Global' }),
                h('option', { value: 'data_zone', text: 'Data zone' }),
                h('option', { value: 'standard', text: 'Standard (in one geography)' }));
            type.value = dep.type || '';
            const azure = h('select', { class: 'form-select mt-2', 'aria-label': 'Azure Region of the resource' },
                regions.filter((r) => r.invocation).map((r) => h('option', { value: r.code.replace('azure:', ''), text: r.name })));
            azure.value = dep.azure_region || '';
            controls = [type, azure];
            read = () => (type.value ? { deployment: { type: type.value, azure_region: azure.value } } : null);
        }
        holder.replaceChildren(intro, ...controls, h('div', { class: 'mt-3' }, sources));
        return read;
    }

    const FOUNDRY = new Set(['anthropic_foundry', 'openai_foundry']);

    // The overlay fields of a declaration; modelId is null when the id is typed in.
    function declareForm(holder, item, modelId) {
        const error = alertBox();
        const field = (label, input, help) => h('div', { class: 'col-md-4' }, h('label', { class: 'form-label', text: label }), input,
            help ? h('div', { class: 'form-text', text: help }) : null);
        const number = (placeholder) => h('input', { type: 'number', min: '1', class: 'form-control form-control-sm', placeholder });
        const foundry = FOUNDRY.has(item.service);
        const typed = modelId === null ? h('input', {
            type: 'text', class: 'form-control form-control-sm conn-model', maxlength: '256', spellcheck: 'false', autocomplete: 'off',
            placeholder: foundry ? 'the deployment name' : 'as the service names it', 'aria-label': 'Model id',
        }) : null;
        const kind = h('select', { class: 'form-select form-select-sm' }, h('option', { value: 'chat', text: 'Chat' }), h('option', { value: 'embedding', text: 'Embedding' }));
        const context = number('for example 32768');
        const output = number('for example 4096');
        const scope = h('select', { class: 'form-select form-select-sm' }, scopes.map((s) => h('option', { value: s, text: SCOPE_LABELS[s] })));
        scope.value = item.kind === 'ollama' ? 'local' : 'in_region';
        const regions = h('input', { type: 'text', class: 'form-control form-control-sm', placeholder: 'Regions, comma separated' });
        if (scope.value === 'in_region' && item.region) regions.value = item.region;
        const flags = ['tools', 'vision', 'prompt_caching', 'structured_output'].map((name) => {
            const box = h('input', { type: 'checkbox', class: 'form-check-input', value: name });
            return { name, box, el: h('div', { class: 'form-check form-check-inline' }, box, h('label', { class: 'form-check-label', text: name.replace('_', ' ') })) };
        });
        const form = h('div', { class: 'conn-declare', hidden: true }, error,
            h('div', { class: 'row g-2' },
                typed ? field(foundry ? 'Model id (deployment name)' : 'Model id', typed,
                    foundry ? 'The name of the deployment on the Foundry resource, exactly as it is shown there.' : null) : null,
                field('Kind', kind), field('Context window', context), field('Output limit', output),
                field('Residency scope', scope, foundry ? 'Where the Foundry resource processes requests.' : null),
                field('Regions', regions)),
            h('div', { class: 'my-2' }, flags.map((f) => f.el)));
        const save = h('button', { type: 'button', class: 'btn btn-app-primary btn-sm', text: 'Save to catalogue' });
        form.appendChild(save);
        save.addEventListener('click', async () => {
            error.hidden = true;
            const id = typed ? typed.value.trim() : modelId;
            if (!id) { showError(error, 'Give the model id, as the service names it.'); typed.focus(); return; }
            const body = {
                model_id: id, kind: kind.value, context_window: context.value, output_limit: output.value,
                residency_scope: scope.value, regions: regions.value,
            };
            for (const f of flags) body[f.name] = f.box.checked;
            save.disabled = true;
            try {
                renderModels(holder, item, await call('POST', '/' + encodeURIComponent(item.id) + '/overlays', body));
                load();
                if (body.kind === 'embedding') toast('success', 'Declared', id + ' is in SPARK\'s catalogue. Embedding models are not listed in this view.');
            } catch (err) { showError(error, err.message); }
            finally { save.disabled = false; }
        });
        return { form, focus: () => (typed || kind).focus() };
    }

    function declareRow(holder, item, model) {
        const { form } = declareForm(holder, item, model.model_id);
        const open = h('button', { type: 'button', class: 'btn btn-app-outline btn-sm', text: 'Declare', onclick: () => { form.hidden = !form.hidden; } });
        return h('div', { class: 'conn-undeclared', 'data-model': model.model_id },
            h('div', { class: 'd-flex align-items-center justify-content-between' },
                h('span', {}, h('span', { class: 'conn-model', text: model.model_id }), model.label ? h('span', { class: 'conn-muted ms-2', text: model.label }) : null),
                open),
            form);
    }

    // Declare a model by typing its id: needs no discovery, and is the only way to add
    // a Foundry deployment (its model id is the deployment name).
    function declareTyped(holder, item, data) {
        const foundry = FOUNDRY.has(item.service);
        const { form, focus } = declareForm(holder, item, null);
        const open = h('button', {
            type: 'button', class: 'btn btn-app-outline btn-sm', 'data-act': 'declare-typed',
            onclick: () => { form.hidden = !form.hidden; if (!form.hidden) focus(); },
        }, icon('bi-plus-lg'), ' Declare a model');
        const usable = data.models.some((m) => !m.refused);
        const hint = foundry
            ? 'A Foundry connection offers only the deployments you declare. Declare each deployment by its name, with the limits and residency of the model it runs.'
            : 'Add a model the catalogue lacks by its id. Check its limits with the service first.';
        return h('div', { class: 'conn-declare-typed mt-4' },
            h('div', { class: 'd-flex align-items-center justify-content-between gap-2' },
                h('h6', { class: 'mb-0', text: foundry ? 'Deployments' : 'Add a model by its id' }), open),
            h('p', { class: 'conn-muted mb-1', text: hint }),
            foundry && !usable ? h('p', { class: 'conn-muted mb-1', text: 'No model is offered on this connection yet.' }) : null,
            form);
    }

    // -- metering (server) --------------------------------------------------------

    const RATE_FIELDS = [
        ['base_rate_tokens', 'Base rate (weighted tokens per credit on a standard model)', '1'],
        ['higher_multiplier', 'Higher tier multiplier', '0.01'],
        ['lower_multiplier', 'Lower tier multiplier', '0.01'],
        ['input_weight', 'Input token weight', '0.01'],
        ['output_weight', 'Output token weight', '0.01'],
        ['cache_read_weight', 'Cache read token weight', '0.01'],
        ['cache_write_weight', 'Cache write token weight', '0.01'],
    ];

    async function openMetering(item) {
        const path = '/' + encodeURIComponent(item.id) + '/metering';
        let data;
        try { data = await call('GET', path); } catch (err) { toast('danger', 'Could not open', err.message); return; }
        const error = alertBox();
        const metered = h('input', { type: 'checkbox', class: 'form-check-input', role: 'switch', id: 'conn-metered' });
        const inputs = {};
        const rows = RATE_FIELDS.map(([name, label, step]) => {
            inputs[name] = h('input', { type: 'number', step, min: '0', class: 'form-control form-control-sm', id: 'conn-m-' + name });
            return h('div', { class: 'col-md-6' }, h('label', { class: 'form-label', for: 'conn-m-' + name, text: label }), inputs[name]);
        });
        const example = h('div', { class: 'conn-example' });
        function fill(values) {
            metered.checked = values.metered;
            for (const [name] of RATE_FIELDS) inputs[name].value = values[name];
        }
        function renderExample(values) {
            const s = values.sample;
            const e = values.example;
            example.replaceChildren(
                h('h6', { text: 'Worked example' }),
                h('p', { class: 'conn-muted', text: 'A sample turn of ' + s.input_tokens + ' input, ' + s.output_tokens + ' output, ' +
                    s.cache_read_tokens + ' cache read and ' + s.cache_write_tokens + ' cache write tokens weighs ' + e.weighted_tokens +
                    ' weighted tokens, at the saved rates:' }),
                h('table', { class: 'app-table conn-table' },
                    h('thead', {}, h('tr', {}, tiers.map((t) => h('th', { text: TIER_LABELS[t] + ' model' })))),
                    h('tbody', {}, h('tr', {}, tiers.map((t) => h('td', { text: e.credits[t] + ' credits' }))))));
        }
        fill(data);
        renderExample(data);
        const reset = h('button', { type: 'button', class: 'btn btn-app-ghost btn-sm', text: 'Reset to defaults',
            onclick: () => { for (const [name] of RATE_FIELDS) inputs[name].value = data.defaults[name]; } });
        const body = h('div', {}, error,
            h('div', { class: 'form-check form-switch mb-3' }, metered, h('label', { class: 'form-check-label', for: 'conn-metered', text: 'Metered: users are held to their monthly credit allowance' })),
            h('div', { class: 'row g-3 mb-2' }, rows), reset, h('hr'), example);
        const save = h('button', { type: 'button', class: 'btn btn-app-primary', text: 'Save' });
        dialog('Metering of ' + item.name, body, [h('button', { type: 'button', class: 'btn btn-app-ghost', 'data-bs-dismiss': 'modal', text: 'Close' }), save], 'modal-lg');
        save.addEventListener('click', async () => {
            error.hidden = true;
            const payload = { metered: metered.checked };
            for (const [name] of RATE_FIELDS) payload[name] = inputs[name].value;
            try {
                data = await call('PUT', path, payload);
                fill(data);
                renderExample(data);
                toast('success', 'Metering saved', item.name);
                load();
            } catch (err) { showError(error, err.message); }
        });
    }

    // -- assignments (server) -----------------------------------------------------

    async function openAssignments(item) {
        const path = '/' + encodeURIComponent(item.id) + '/assignments';
        let data;
        try { data = await call('GET', path); } catch (err) { toast('danger', 'Could not open', err.message); return; }
        const error = alertBox();
        const holder = h('div', {});
        const kind = h('select', { class: 'form-select form-select-sm', id: 'conn-a-kind' },
            h('option', { value: 'role', text: 'A role' }), h('option', { value: 'user', text: 'A named user' }));
        const target = h('select', { class: 'form-select form-select-sm', id: 'conn-a-target' });
        const allowance = h('input', { type: 'number', min: '0', step: '1', class: 'form-control form-control-sm', id: 'conn-a-credits', placeholder: 'Credits each month' });
        const noLimit = h('input', { type: 'checkbox', class: 'form-check-input', id: 'conn-a-nolimit' });
        const requirement = h('div', { class: 'form-text' });
        function targets() {
            const list = kind.value === 'role'
                ? data.roles.map((r) => h('option', { value: r, text: r }))
                : data.users.map((u) => h('option', { value: u.user_guid, text: u.label }));
            target.replaceChildren(...list);
        }
        function limits() {
            allowance.disabled = noLimit.checked;
            if (noLimit.checked) allowance.value = '';
            requirement.textContent = data.metered
                ? 'This connection is metered: give a monthly allowance in credits, or tick No limit.'
                : 'This connection is not metered, so nothing is limited. An allowance applies once it is metered.';
        }
        kind.addEventListener('change', targets);
        noLimit.addEventListener('change', limits);
        function render() {
            const rows = data.assignments.map((a) => {
                const who = a.role ? 'Role: ' + a.role : 'User: ' + (a.user || a.user_guid);
                const credits = a.no_limit ? 'No limit' : a.monthly_credits + ' credits a month';
                const query = a.role ? 'role=' + encodeURIComponent(a.role) : 'user_guid=' + encodeURIComponent(a.user_guid);
                const drop = h('button', { type: 'button', class: 'btn btn-app-ghost btn-sm', 'aria-label': 'Remove ' + who }, icon('bi-x-lg'));
                drop.addEventListener('click', async () => {
                    try { data = await call('DELETE', path + '?' + query); render(); load(); }
                    catch (err) { showError(error, err.message); }
                });
                return h('tr', {}, h('td', { text: who }), h('td', { text: credits }), h('td', { class: 'text-end' }, drop));
            });
            holder.replaceChildren(rows.length
                ? h('table', { class: 'app-table conn-table' }, h('tbody', {}, rows))
                : h('p', { class: 'conn-muted', text: 'Not assigned: only administrators can see this connection.' }));
            limits();
        }
        targets();
        render();
        const add = h('button', { type: 'button', class: 'btn btn-app-primary btn-sm', text: 'Assign' });
        add.addEventListener('click', async () => {
            error.hidden = true;
            const body = { monthly_credits: allowance.value, no_limit: noLimit.checked };
            body[kind.value === 'role' ? 'role' : 'user_guid'] = target.value;
            try { data = await call('PUT', path, body); render(); load(); }
            catch (err) { showError(error, err.message); }
        });
        const body = h('div', {}, error,
            h('ul', { class: 'conn-rules' },
                h('li', { text: 'A user\'s own assignment overrides their roles\' assignments.' }),
                h('li', { text: 'Where several of a user\'s roles are assigned, the highest allowance applies.' }),
                h('li', { text: 'A connection assigned to no one is hidden from everyone but administrators.' })),
            holder, h('hr'),
            h('div', { class: 'row g-2 align-items-end' },
                h('div', { class: 'col-md-3' }, h('label', { class: 'form-label', for: 'conn-a-kind', text: 'Assign to' }), kind),
                h('div', { class: 'col-md-4' }, h('label', { class: 'form-label', for: 'conn-a-target', text: 'Who' }), target),
                h('div', { class: 'col-md-3' }, h('label', { class: 'form-label', for: 'conn-a-credits', text: 'Monthly allowance' }), allowance),
                h('div', { class: 'col-md-2' }, h('div', { class: 'form-check' }, noLimit, h('label', { class: 'form-check-label', for: 'conn-a-nolimit', text: 'No limit' })))),
            requirement, h('div', { class: 'mt-2' }, add));
        dialog('Assignments of ' + item.name, body, h('button', { type: 'button', class: 'btn btn-app-ghost', 'data-bs-dismiss': 'modal', text: 'Close' }), 'modal-lg');
    }

    // -- start --------------------------------------------------------------------

    async function init() {
        root = document.getElementById('connections-app');
        if (!root) return;
        server = root.dataset.server === 'true';
        const picker = h('select', { class: 'form-select form-select-sm conn-add', 'aria-label': 'Add connection' },
            h('option', { value: '', text: 'Add connection...' }));
        root.replaceChildren(h('div', { class: 'd-flex justify-content-end mb-3' }, picker), h('div', { class: 'conn-list' }));
        try {
            const data = await call('GET', '/services');
            services = data.services;
            scopes = data.scopes;
            tiers = data.tiers;
            for (const s of services) picker.appendChild(h('option', { value: s.key, text: s.label }));
        } catch (err) {
            root.querySelector('.conn-list').replaceChildren(h('div', { class: 'alert-app alert-app-warning', text: err.message }));
            return;
        }
        picker.addEventListener('change', () => {
            const key = picker.value;
            picker.value = '';
            if (key) openModal(key, null);
        });
        await load();
    }

    window.SparkConnections = { init, reload: () => load() };
    document.addEventListener('DOMContentLoaded', init);
})();
