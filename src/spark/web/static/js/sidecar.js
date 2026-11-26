/**
 * Spark: the conversation's right-hand panel (PC2 to PC5).
 *
 * One panel with three collapsible sections: Artefacts, Tools and Agents. Tool calls and
 * agent runs are drawn by chat.js into the section bodies it has always used
 * (#tool-panel-body, #agent-panel-body); this file owns the panel itself: the sections,
 * their counts and remembered state, opening and resizing, the dot on its button, and the
 * list of artefacts.
 *
 * Artefacts come from GET /artefacts/api/<conversation>, in three groups: generated or
 * retrieved, provided (by you, or by each person in a shared conversation) and the
 * project's resources. An item opens in the document preview pane (preview.js) and
 * scrolls to the message that produced it; the owner and a Contributor rename an item in
 * place (PATCH /artefacts/api/<file>). Everything shown is set as text, never as markup.
 *
 * Behaviour (PC4): Artefacts open and Tools and Agents closed when a conversation opens;
 * a section expands while something runs in it and then stays as the user leaves it; the
 * user's own choice per section is remembered in the browser (every storage call is
 * guarded, so the panel works when storage throws); the panel opens by itself the first
 * time an artefact appears in a conversation; its button shows a dot for new items; at
 * phone width it is a full-screen overlay. While the preview pane is open the panel
 * narrows (or, at phone width, steps aside) and returns when the pane closes.
 *
 * The logic part (no DOM) exports itself under Node for tests.
 */
(function (root) {
    'use strict';

    const SECTIONS = ['artefacts', 'tools', 'agents'];
    const DEFAULT_OPEN = { artefacts: true, tools: false, agents: false };
    const SECTION_PREFIX = 'spark-sidecar-section-';
    const SEEN_PREFIX = 'spark-sidecar-seen-';
    const PANEL_KEY = 'spark-tool-panel';
    const WIDTH_KEY = 'spark-sidecar-width';
    const API = '/artefacts/api/';
    const TITLE_LIMIT = 120;
    const TYPE_LABELS = {
        pdf: 'PDF',
        docx: 'Word document',
        sheet: 'Spreadsheet',
        image: 'Image',
        markdown: 'Markdown',
        text: 'Text',
        chart: 'Chart',
        diagram: 'Diagram',
        other: 'File',
    };

    // -- Guarded storage ----------------------------------------------------------------

    function readItem(storage, key) {
        if (!storage) return null;
        try {
            return storage.getItem(key);
        } catch (err) {
            return null;
        }
    }

    function writeItem(storage, key, value) {
        if (!storage) return;
        try {
            storage.setItem(key, value);
        } catch (err) {
            /* storage blocked: the choice lasts for this page only */
        }
    }

    // -- The model ----------------------------------------------------------------------

    /**
     * The panel's state: which sections are open, their counts, whether the panel is open,
     * the dot, and how the panel sits beside the preview pane. ``storage`` keeps the
     * user's choice per section and which conversations have shown their first artefact;
     * ``session`` keeps whether the panel was open in this tab.
     */
    function createModel(options) {
        const opts = options || {};
        const storage = opts.storage || null;
        const session = opts.session || null;
        const model = {
            sections: {},
            panelOpen: readItem(session, PANEL_KEY) === 'open',
            dot: false,
            paneOpen: false,
            phone: !!opts.phone,
            seen: readItem(storage, SEEN_PREFIX + opts.conversationId) === '1',
            known: new Set(),
        };
        SECTIONS.forEach(name => {
            const saved = readItem(storage, SECTION_PREFIX + name);
            const open = saved === 'open' ? true : saved === 'closed' ? false : DEFAULT_OPEN[name];
            model.sections[name] = { open, count: 0 };
        });

        model.isOpen = name => !!(model.sections[name] && model.sections[name].open);
        model.count = name => (model.sections[name] ? model.sections[name].count : 0);
        model.setCount = (name, value) => {
            if (!model.sections[name]) return;
            const n = Math.floor(Number(value));
            model.sections[name].count = n > 0 ? n : 0;
        };
        /** The user opens or closes a section: remembered. */
        model.toggle = name => {
            if (!model.sections[name]) return false;
            const section = model.sections[name];
            section.open = !section.open;
            writeItem(storage, SECTION_PREFIX + name, section.open ? 'open' : 'closed');
            return section.open;
        };
        /** Something runs in a section: it expands (not remembered), then stays. */
        model.run = name => {
            if (!model.sections[name] || model.sections[name].open) return false;
            model.sections[name].open = true;
            return true;
        };
        model.openPanel = () => {
            model.panelOpen = true;
            model.dot = false;
            writeItem(session, PANEL_KEY, 'open');
        };
        model.closePanel = () => {
            model.panelOpen = false;
            writeItem(session, PANEL_KEY, 'closed');
        };
        model.togglePanel = () => {
            if (model.panelOpen) model.closePanel();
            else model.openPanel();
            return model.panelOpen;
        };
        /** A new tool call or agent run: the dot, when the panel is closed. */
        model.noteItem = () => {
            if (!model.panelOpen) model.dot = true;
        };
        /**
         * The artefacts now listed (ids). The first artefact a conversation ever shows
         * opens the panel; later ones, appearing while it is closed, light the dot. The
         * first listing of a page (``initial``) never lights the dot.
         */
        model.noteArtefacts = (ids, initial) => {
            const list = Array.isArray(ids) ? ids : [];
            const fresh = list.filter(id => !model.known.has(id));
            list.forEach(id => model.known.add(id));
            const result = { opened: false, dot: false };
            if (list.length && !model.seen) {
                model.seen = true;
                writeItem(storage, SEEN_PREFIX + opts.conversationId, '1');
                if (!model.panelOpen) {
                    model.openPanel();
                    result.opened = true;
                }
            } else if (fresh.length && !initial && !model.panelOpen) {
                model.dot = true;
                result.dot = true;
            }
            return result;
        };
        model.setPane = open => { model.paneOpen = !!open; };
        model.setPhone = phone => { model.phone = !!phone; };
        /** hidden, full, narrow (beside the preview pane) or aside (out of its way). */
        model.layout = () => {
            if (!model.panelOpen) return 'hidden';
            if (!model.paneOpen) return 'full';
            return model.phone ? 'aside' : 'narrow';
        };
        return model;
    }

    // -- The listing --------------------------------------------------------------------

    function asList(value) {
        return Array.isArray(value) ? value : [];
    }

    /** The groups the panel draws: generated, provided, then one per project level. */
    function groupsOf(payload) {
        const data = payload && typeof payload === 'object' ? payload : {};
        const generated = asList(data.generated);
        const provided = asList(data.provided);
        const groups = [
            { id: 'generated', label: 'Generated or retrieved', items: generated, count: generated.length },
            {
                id: 'provided',
                label: data.shared ? 'Provided' : 'Provided by you',
                items: provided,
                count: provided.length,
            },
        ];
        asList(data.project).forEach(level => {
            const items = asList(level && level.items);
            if (!items.length) return;
            groups.push({
                id: 'project-' + level.project_id,
                label: 'Project resources: ' + String(level.name || ''),
                items,
                count: items.length,
            });
        });
        return groups;
    }

    function totalOf(payload) {
        return groupsOf(payload).reduce((sum, group) => sum + group.count, 0);
    }

    function formatSize(bytes) {
        const n = Number(bytes) || 0;
        if (n < 1024) return n.toLocaleString() + ' bytes';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / (1024 * 1024)).toFixed(1) + ' MB';
    }

    /** A stored time as a Date (database times without a zone are UTC), or null. */
    function parseWhen(value) {
        if (!value) return null;
        let text = String(value);
        if (/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(text)) text = text.replace(' ', 'T') + 'Z';
        const date = new Date(text);
        return Number.isNaN(date.getTime()) ? null : date;
    }

    /** What a row says about an item: title, then file name, type, size, time and who. */
    function itemMeta(item, locale) {
        const where = locale || {};
        const date = parseWhen(item.created_at);
        let when = '';
        if (date) {
            const format = { day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit' };
            if (where.timeZone) format.timeZone = where.timeZone;
            when = date.toLocaleString(where.locale || undefined, format);
        }
        const by = item.provided_by
            ? 'Attached by ' + (item.provided_by.is_me ? 'you' : String(item.provided_by.name || 'someone'))
            : '';
        return {
            title: String(item.title || item.name || 'file'),
            detail: String(item.name || ''),
            type: TYPE_LABELS[item.preview_kind] || TYPE_LABELS.other,
            size: formatSize(item.size),
            when,
            by,
        };
    }

    /** The card the preview pane opens for an item, or null. */
    function cardOf(item, conversationId) {
        if (!item || typeof item !== 'object') return null;
        const fileId = Number(item.file_id);
        const conversation = Number(conversationId);
        if (!Number.isInteger(fileId) || !Number.isInteger(conversation)) return null;
        return {
            file_id: fileId,
            conversation_id: conversation,
            name: String(item.name || 'document'),
            title: String(item.title || item.name || 'document'),
            type: String(item.type || ''),
            kind: TYPE_LABELS[item.preview_kind] ? String(item.preview_kind) : 'other',
            size: Number(item.size) || 0,
        };
    }

    /** The message to scroll to: the one named, else the nearest earlier one that is drawn. */
    function nearestMessage(messageId, available) {
        const target = Number(messageId);
        if (messageId === null || messageId === undefined || !Number.isFinite(target)) return null;
        let best = null;
        asList(available).forEach(id => {
            if (id <= target && (best === null || id > best)) best = id;
        });
        return best;
    }

    /** A title checked before it is sent: trimmed, not blank, not too long. */
    function checkTitle(text) {
        if (typeof text !== 'string') return { ok: false, error: 'A title is required.' };
        const title = text.trim();
        if (!title) return { ok: false, error: 'A title is required.' };
        if (title.length > TITLE_LIMIT) {
            return { ok: false, error: 'The title is longer than ' + TITLE_LIMIT + ' characters.' };
        }
        return { ok: true, title };
    }

    function listUrl(conversationId) {
        return API + encodeURIComponent(conversationId);
    }

    function renameUrl(fileId) {
        return API + encodeURIComponent(fileId);
    }

    const logic = {
        SECTIONS,
        DEFAULT_OPEN,
        createModel,
        groupsOf,
        totalOf,
        itemMeta,
        cardOf,
        nearestMessage,
        checkTitle,
        listUrl,
        renameUrl,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = logic;
        return;
    }

    // -- The page -------------------------------------------------------------------

    const KIND_ICONS = {
        pdf: 'bi-file-earmark-pdf',
        docx: 'bi-file-earmark-word',
        sheet: 'bi-file-earmark-spreadsheet',
        image: 'bi-file-earmark-image',
        markdown: 'bi-file-earmark-richtext',
        text: 'bi-file-earmark-text',
        chart: 'bi-bar-chart-line',
        diagram: 'bi-diagram-3',
        other: 'bi-file-earmark',
    };
    const REFRESH_DELAY = 250;
    const MIN_WIDTH = 250;
    const MAX_WIDTH = 800;
    const PHONE = window.matchMedia ? window.matchMedia('(max-width: 767.98px)') : null;

    const state = {
        model: null,
        conversationId: null,
        loaded: false,
        data: null,
        timer: null,
        editing: false,
        pending: false,
        returnFocus: null,
        origin: null,
    };

    function $(id) { return document.getElementById(id); }

    function browserStorage(kind) {
        try {
            return window[kind];
        } catch (err) {
            return null;
        }
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function icon(name) {
        const node = document.createElement('i');
        node.className = 'bi ' + name;
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    // Applying the model to the page: attributes only, so focus stays where it is.
    function apply() {
        const model = state.model;
        if (!model) return;
        SECTIONS.forEach(name => {
            const toggle = $('sidecar-' + name + '-toggle');
            const body = $('sidecar-' + name + '-body');
            const open = model.isOpen(name);
            if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (body) body.hidden = !open;
            const count = $('sidecar-' + name + '-count');
            if (count) count.textContent = String(model.count(name));
        });
        const panel = $('tool-panel');
        const layout = model.layout();
        if (panel) {
            panel.hidden = layout === 'hidden' || layout === 'aside';
            panel.dataset.layout = layout;
            const overlay = model.phone && model.panelOpen;
            panel.setAttribute('role', overlay ? 'dialog' : 'complementary');
            panel.setAttribute('aria-modal', overlay ? 'true' : 'false');
        }
        const button = $('btn-tool-panel');
        if (button) {
            button.setAttribute('aria-expanded', model.panelOpen ? 'true' : 'false');
            button.classList.toggle('active', model.panelOpen);
        }
        const dot = $('sidecar-dot');
        if (dot) dot.hidden = !model.dot;
    }

    function openPanel(focus) {
        const model = state.model;
        if (!model) return;
        if (!model.panelOpen) state.returnFocus = document.activeElement;
        model.openPanel();
        apply();
        if (focus && model.phone) {
            const title = $('sidecar-title');
            if (title) title.focus();
        }
    }

    function closePanel() {
        const model = state.model;
        if (!model || !model.panelOpen) return;
        model.closePanel();
        apply();
        const back = state.returnFocus || $('btn-tool-panel');
        state.returnFocus = null;
        if (back && document.contains(back) && typeof back.focus === 'function') back.focus();
    }

    function togglePanel() {
        if (!state.model) return;
        if (state.model.panelOpen) closePanel();
        else openPanel(true);
    }

    function toggleSection(name) {
        if (!state.model) return;
        state.model.toggle(name);
        apply();
    }

    /** Something runs in a section (a tool call, an agent): it expands, and the button shows a dot. */
    function running(name) {
        if (!state.model) return;
        state.model.run(name);
        state.model.noteItem(name);
        apply();
    }

    /** Show a section: open the panel and expand the section (not remembered as a choice). */
    function reveal(name) {
        if (!state.model) return;
        state.model.run(name);
        openPanel(true);
    }

    function setCount(name, value) {
        if (!state.model) return;
        state.model.setCount(name, value);
        apply();
    }

    // -- Opening an item and finding its message ----------------------------------------

    function messageElements() {
        const found = [];
        document.querySelectorAll('#chat-messages [data-message-id]').forEach(node => {
            const id = Number(node.dataset.messageId);
            if (Number.isFinite(id)) found.push({ id, node });
        });
        return found;
    }

    function scrollToMessage(messageId) {
        const drawn = messageElements();
        const id = nearestMessage(messageId, drawn.map(entry => entry.id));
        if (id === null) return false;
        const target = drawn.find(entry => entry.id === id).node;
        const calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        target.scrollIntoView({ block: 'center', behavior: calm ? 'auto' : 'smooth' });
        document.querySelectorAll('.sidecar-origin').forEach(node => node.classList.remove('sidecar-origin'));
        target.classList.add('sidecar-origin');
        window.setTimeout(() => target.classList.remove('sidecar-origin'), 2400);
        return true;
    }

    function openItem(item, from) {
        const card = cardOf(item, state.conversationId);
        if (card && window.SparkPreview) window.SparkPreview.open(card, { focus: true, from });
        if (item.message_id !== null && item.message_id !== undefined) scrollToMessage(item.message_id);
    }

    // -- Rendering the artefacts --------------------------------------------------------

    function rowText(item) {
        const meta = itemMeta(item);
        const text = el('span', 'sidecar-item-text');
        text.appendChild(el('span', 'sidecar-item-title', meta.title));
        text.appendChild(el('span', 'sidecar-item-detail', [meta.detail, meta.type, meta.size].filter(Boolean).join(' \u00b7 ')));
        const line = [meta.by, meta.when].filter(Boolean).join(' \u00b7 ');
        if (line) text.appendChild(el('span', 'sidecar-item-when', line));
        return { text, meta };
    }

    function renderItem(item, group) {
        const row = el('li', 'sidecar-item');
        row.dataset.fileId = String(item.file_id);
        const open = el('button', 'sidecar-open');
        open.type = 'button';
        const { text, meta } = rowText(item);
        open.setAttribute('aria-label', 'Open ' + meta.title + ' in the document preview');
        open.title = meta.title;
        open.appendChild(icon(KIND_ICONS[item.preview_kind] || KIND_ICONS.other));
        open.appendChild(text);
        open.addEventListener('click', () => openItem(item, open));
        row.appendChild(open);
        if (item.can_rename) {
            const rename = el('button', 'btn btn-app-ghost btn-sm sidecar-rename');
            rename.type = 'button';
            rename.setAttribute('aria-label', 'Rename ' + meta.title);
            rename.title = 'Rename';
            rename.appendChild(icon('bi-pencil'));
            rename.addEventListener('click', () => startRename(row, item, group));
            row.appendChild(rename);
        }
        return row;
    }

    function renderGroups(payload) {
        const list = $('sidecar-artefacts-list');
        if (!list) return;
        list.replaceChildren();
        const groups = groupsOf(payload);
        if (!totalOf(payload)) {
            list.appendChild(el('p', 'sidecar-empty', 'No artefacts yet. Documents, charts and files for this conversation appear here.'));
            return;
        }
        groups.forEach(group => {
            if (!group.items.length) return;
            const section = el('div', 'sidecar-group');
            const heading = el('h4', 'sidecar-group-title', group.label);
            heading.appendChild(el('span', 'sidecar-group-count', ' (' + group.count + ')'));
            section.appendChild(heading);
            const items = el('ul', 'sidecar-items');
            group.items.forEach(item => items.appendChild(renderItem(item, group)));
            section.appendChild(items);
            list.appendChild(section);
        });
    }

    function render() {
        if (state.editing) {
            state.pending = true;
            return;
        }
        state.pending = false;
        renderGroups(state.data);
        setCount('artefacts', totalOf(state.data));
    }

    // -- Rename ---------------------------------------------------------------------------

    function startRename(row, item) {
        if (state.editing) return;
        state.editing = true;
        const open = row.querySelector('.sidecar-open');
        const rename = row.querySelector('.sidecar-rename');
        open.hidden = true;
        rename.hidden = true;
        const form = el('form', 'sidecar-rename-form');
        const input = el('input', 'form-control form-control-sm');
        input.type = 'text';
        input.value = String(item.title || '');
        input.maxLength = TITLE_LIMIT;
        input.setAttribute('aria-label', 'Title');
        const save = el('button', 'btn btn-app-primary btn-sm', 'Save');
        save.type = 'submit';
        const cancel = el('button', 'btn btn-app-ghost btn-sm', 'Cancel');
        cancel.type = 'button';
        const error = el('div', 'sidecar-rename-error');
        error.setAttribute('role', 'alert');
        error.hidden = true;
        form.append(input, save, cancel, error);
        row.appendChild(form);
        input.focus();
        input.select();

        const finish = restore => {
            state.editing = false;
            form.remove();
            open.hidden = false;
            rename.hidden = false;
            if (restore) rename.focus();
            if (state.pending) render();
        };
        cancel.addEventListener('click', () => finish(true));
        form.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                event.stopPropagation();
                finish(true);
            }
        });
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const checked = checkTitle(input.value);
            if (!checked.ok) {
                error.textContent = checked.error;
                error.hidden = false;
                input.focus();
                return;
            }
            save.disabled = true;
            try {
                const resp = await fetch(renameUrl(item.file_id), {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ title: checked.title }),
                });
                const body = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    error.textContent = body.error || 'The title could not be changed.';
                    error.hidden = false;
                    save.disabled = false;
                    input.focus();
                    return;
                }
                item.title = body.title;
                finish(true);
                refresh({ now: true });
            } catch (err) {
                error.textContent = 'The title could not be changed.';
                error.hidden = false;
                save.disabled = false;
            }
        });
    }

    // -- Loading --------------------------------------------------------------------------

    async function load() {
        if (state.conversationId === null) return;
        try {
            const resp = await fetch(listUrl(state.conversationId), { credentials: 'same-origin' });
            if (!resp.ok) return;
            const data = await resp.json();
            const initial = !state.loaded;
            state.loaded = true;
            state.data = data;
            const ids = groupsOf(data).flatMap(group => group.items.map(item => item.file_id));
            state.model.noteArtefacts(ids, initial);
            render();
            apply();
        } catch (err) {
            /* the list is not essential: the next event asks again */
        }
    }

    /** Ask for the list again, once for a burst of events. */
    function refresh(options) {
        if (state.conversationId === null) return;
        if (state.timer) window.clearTimeout(state.timer);
        const delay = options && options.now ? 0 : REFRESH_DELAY;
        state.timer = window.setTimeout(() => {
            state.timer = null;
            load();
        }, delay);
    }

    // -- Resizing -------------------------------------------------------------------------

    function applyWidth(width) {
        const panel = $('tool-panel');
        if (!panel) return null;
        const value = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(width)));
        panel.style.width = value + 'px';
        const handle = $('sidecar-resize-handle');
        if (handle) {
            handle.setAttribute('aria-valuemin', String(MIN_WIDTH));
            handle.setAttribute('aria-valuemax', String(MAX_WIDTH));
            handle.setAttribute('aria-valuenow', String(value));
        }
        return value;
    }

    function saveWidth(value) {
        writeItem(browserStorage('sessionStorage'), WIDTH_KEY, value + 'px');
    }

    function startSidecarResize(event) {
        if (PHONE && PHONE.matches) return;
        const panel = $('tool-panel');
        const startX = event.clientX;
        const startWidth = panel.offsetWidth;
        panel.classList.add('sidecar-resizing');
        // Dragging left makes the panel wider, right narrower.
        const move = e => applyWidth(startWidth + (startX - e.clientX));
        const up = () => {
            panel.classList.remove('sidecar-resizing');
            saveWidth(panel.offsetWidth);
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', up);
        };
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseup', up);
        event.preventDefault();
    }

    function keyResize(event) {
        const step = event.shiftKey ? 96 : 32;
        const width = $('tool-panel').offsetWidth;
        let value = null;
        if (event.key === 'ArrowLeft') value = applyWidth(width + step);
        else if (event.key === 'ArrowRight') value = applyWidth(width - step);
        if (value !== null) {
            event.preventDefault();
            saveWidth(value);
        }
    }

    // -- Starting -------------------------------------------------------------------------

    function watchPane() {
        const pane = $('preview-pane');
        if (!pane) return;
        const sync = () => {
            state.model.setPane(!pane.hidden);
            apply();
        };
        sync();
        if (window.MutationObserver) {
            new MutationObserver(sync).observe(pane, { attributes: true, attributeFilter: ['hidden'] });
        }
    }

    function init(options) {
        const opts = options || {};
        state.conversationId = Number(opts.conversationId);
        state.model = createModel({
            storage: browserStorage('localStorage'),
            session: browserStorage('sessionStorage'),
            conversationId: state.conversationId,
            phone: !!(PHONE && PHONE.matches),
        });
        SECTIONS.forEach(name => {
            const toggle = $('sidecar-' + name + '-toggle');
            if (toggle) toggle.addEventListener('click', () => toggleSection(name));
        });
        const close = $('sidecar-close');
        if (close) close.addEventListener('click', closePanel);
        const button = $('btn-tool-panel');
        if (button) button.addEventListener('click', togglePanel);
        const handle = $('sidecar-resize-handle');
        if (handle) {
            handle.addEventListener('mousedown', startSidecarResize);
            handle.addEventListener('keydown', keyResize);
        }
        const panel = $('tool-panel');
        if (panel) {
            panel.addEventListener('keydown', event => {
                if (event.key === 'Escape' && state.model.phone && !state.editing) {
                    event.preventDefault();
                    closePanel();
                }
            });
        }
        const saved = readItem(browserStorage('sessionStorage'), WIDTH_KEY);
        if (saved) applyWidth(parseInt(saved, 10) || 380);
        if (PHONE && PHONE.addEventListener) {
            PHONE.addEventListener('change', () => {
                state.model.setPhone(PHONE.matches);
                apply();
            });
        }
        watchPane();
        apply();
        refresh({ now: true });
    }

    window.SparkSidecar = {
        init,
        refresh,
        running,
        reveal,
        setCount,
        openPanel,
        closePanel,
        togglePanel,
        scrollToMessage,
        _state: state,
    };
    // The page's older names for opening and resizing the panel.
    window.toggleToolPanel = togglePanel;
    window.startSidecarResize = startSidecarResize;
})(typeof window !== 'undefined' ? window : this);
