/**
 * Spark: the document preview pane and document cards (CS39).
 *
 * show_document's result carries a card ({file_id, conversation_id, name, title, type,
 * kind, size}) on a line starting "[spark-preview]". The chat page draws it as a
 * document card in the message; the card opens (or reopens) the document in the pane
 * on the right: one tab per document, each with Download and Close, resizable,
 * closable, keyboard accessible, and a full-screen overlay at phone width.
 *
 * The pane fetches the file from SPARK (/preview/api/<conversation>/files/<file>,
 * which every member of the conversation may read) and hands the bytes to the
 * document's own frame, /preview/frame in an <iframe sandbox="allow-scripts">, which
 * renders it with no access to this page. The PDF renderer is handed over as text,
 * since a sandboxed frame cannot import modules from SPARK.
 */
(function () {
    'use strict';

    const MARKER = '[spark-preview]';
    const MAX_TABS = 8;
    const MIN_WIDTH = 280;
    const WIDTH_KEY = 'spark-preview-width';
    const PHONE = window.matchMedia ? window.matchMedia('(max-width: 767.98px)') : null;
    const KIND_ICONS = {
        pdf: 'bi-file-earmark-pdf',
        docx: 'bi-file-earmark-word',
        sheet: 'bi-file-earmark-spreadsheet',
        image: 'bi-file-earmark-image',
        markdown: 'bi-file-earmark-richtext',
        text: 'bi-file-earmark-text',
        other: 'bi-file-earmark',
    };
    const KIND_LABELS = {
        pdf: 'PDF',
        docx: 'Word document',
        sheet: 'Spreadsheet',
        image: 'Image',
        markdown: 'Markdown',
        text: 'Text',
        other: 'File',
    };

    const state = {
        tabs: [],          // {card, tab, panel, frame, ready, pending}
        active: null,
        known: new Set(),  // file ids drawn as cards on this page
        history: null,     // ids known before a history reload, or null
        returnFocus: null,
        pdf: null,         // Promise of {lib, worker}
    };

    function $(id) { return document.getElementById(id); }

    function esc(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function formatSize(bytes) {
        const n = Number(bytes) || 0;
        if (n < 1024) return `${n.toLocaleString()} bytes`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    }

    function fileUrl(card, download) {
        const base = `/preview/api/${encodeURIComponent(card.conversation_id)}/files/${encodeURIComponent(card.file_id)}`;
        return download ? `${base}?download=1` : base;
    }

    // -- Cards ------------------------------------------------------------------

    /** The card a show_document result carries, or null. */
    function parse(text) {
        const lines = String(text || '').split('\n');
        for (const line of lines) {
            if (!line.startsWith(MARKER)) continue;
            try {
                const data = JSON.parse(line.slice(MARKER.length).trim());
                const card = {
                    file_id: Number(data.file_id),
                    conversation_id: Number(data.conversation_id),
                    name: String(data.name || 'document'),
                    title: String(data.title || data.name || 'document'),
                    type: String(data.type || ''),
                    kind: KIND_LABELS[data.kind] ? String(data.kind) : 'other',
                    size: Number(data.size) || 0,
                };
                if (!Number.isInteger(card.file_id) || !Number.isInteger(card.conversation_id)) return null;
                return card;
            } catch (err) {
                return null;
            }
        }
        return null;
    }

    function pageConversation() {
        const pane = $('preview-pane');
        return pane ? Number(pane.dataset.conversationId) : NaN;
    }

    /** A document card element that opens the document in the pane. */
    function cardElement(card) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'document-card';
        button.dataset.fileId = String(card.file_id);
        button.setAttribute('aria-label', `Open ${card.title} in the document preview`);
        button.innerHTML = `
            <i class="bi ${KIND_ICONS[card.kind] || KIND_ICONS.other} document-card-icon" aria-hidden="true"></i>
            <span class="document-card-text">
                <span class="document-card-name">${esc(card.title)}</span>
                <span class="document-card-meta">${esc(KIND_LABELS[card.kind] || 'File')} · ${esc(formatSize(card.size))}</span>
            </span>
            <i class="bi bi-layout-sidebar-reverse document-card-open" aria-hidden="true"></i>`;
        button.addEventListener('click', () => open(card, { focus: true, from: button }));
        return button;
    }

    /**
     * Draw a card at the end of the conversation. Returns the element, or null when the
     * card is not one of this conversation's.
     */
    function appendCard(card) {
        if (!card || card.conversation_id !== pageConversation()) return null;
        const container = $('chat-messages');
        if (!container) return null;
        const wrapper = document.createElement('div');
        wrapper.className = 'chat-message-wrapper assistant document-card-wrapper';
        const element = cardElement(card);
        wrapper.appendChild(element);
        container.appendChild(wrapper);
        const isNew = !state.known.has(card.file_id);
        state.known.add(card.file_id);
        if (state.history && !state.history.has(card.file_id) && isNew) state.history.opened.push(card);
        return element;
    }

    /** Before a history reload: note which documents were already known. */
    function beginHistory(initial) {
        if (initial) {
            state.history = null;
            return;
        }
        state.history = new Set(state.known);
        state.history.opened = [];
    }

    /** After a history reload: open the documents that appeared (another member's turn). */
    function endHistory() {
        const fresh = state.history ? state.history.opened : [];
        state.history = null;
        fresh.forEach(card => open(card, { focus: false }));
    }

    // -- The pane -----------------------------------------------------------------

    function pane() { return $('preview-pane'); }

    function isPhone() { return !!(PHONE && PHONE.matches); }

    function applyWidth(width) {
        const el = pane();
        if (!el) return;
        const max = Math.max(MIN_WIDTH, Math.floor(window.innerWidth * 0.7));
        const value = Math.min(Math.max(Math.round(width), MIN_WIDTH), max);
        el.style.setProperty('--preview-width', `${value}px`);
        const handle = $('preview-resize');
        if (handle) {
            handle.setAttribute('aria-valuemin', String(MIN_WIDTH));
            handle.setAttribute('aria-valuemax', String(max));
            handle.setAttribute('aria-valuenow', String(value));
        }
        return value;
    }

    function savedWidth() {
        try {
            const value = Number(localStorage.getItem(WIDTH_KEY));
            return value > 0 ? value : Math.floor(window.innerWidth * 0.4);
        } catch (err) {
            return Math.floor(window.innerWidth * 0.4);
        }
    }

    function saveWidth(value) {
        try { localStorage.setItem(WIDTH_KEY, String(value)); } catch (err) { /* private mode */ }
    }

    function showPane() {
        const el = pane();
        if (!el) return;
        if (el.hidden) {
            state.returnFocus = document.activeElement;
            el.hidden = false;
            applyWidth(savedWidth());
        }
        el.classList.toggle('preview-overlay', isPhone());
        el.setAttribute('aria-modal', isPhone() ? 'true' : 'false');
        el.setAttribute('role', isPhone() ? 'dialog' : 'complementary');
    }

    function hidePane() {
        const el = pane();
        if (!el || el.hidden) return;
        el.hidden = true;
        const back = state.returnFocus;
        state.returnFocus = null;
        if (back && document.contains(back) && typeof back.focus === 'function') back.focus();
    }

    function select(entry, focusTab) {
        state.active = entry;
        state.tabs.forEach(other => {
            const on = other === entry;
            other.tab.setAttribute('aria-selected', on ? 'true' : 'false');
            other.tab.tabIndex = on ? 0 : -1;
            other.panel.hidden = !on;
        });
        if (focusTab) entry.tab.focus();
    }

    function closeTab(entry) {
        const index = state.tabs.indexOf(entry);
        if (index < 0) return;
        state.tabs.splice(index, 1);
        entry.tabWrap.remove();
        entry.panel.remove();
        if (!state.tabs.length) {
            state.active = null;
            hidePane();
            return;
        }
        if (state.active === entry) select(state.tabs[Math.min(index, state.tabs.length - 1)], true);
    }

    function closeAll() {
        state.tabs.slice().forEach(entry => {
            entry.tabWrap.remove();
            entry.panel.remove();
        });
        state.tabs = [];
        state.active = null;
        hidePane();
    }

    function pdfRenderer() {
        if (!state.pdf) {
            const text = url => fetch(url, { credentials: 'same-origin' }).then(resp => {
                if (!resp.ok) throw new Error('the PDF renderer could not be loaded');
                return resp.text();
            });
            state.pdf = Promise.all([
                text('/static/vendor/pdfjs/pdf.min.mjs'),
                text('/static/vendor/pdfjs/pdf.worker.min.mjs'),
            ]).then(([lib, worker]) => ({ lib, worker })).catch(err => {
                state.pdf = null;
                throw err;
            });
        }
        return state.pdf;
    }

    function setNotice(entry, text, isError) {
        entry.notice.textContent = text || '';
        entry.notice.hidden = !text;
        entry.notice.classList.toggle('preview-notice-error', !!isError);
    }

    async function load(entry) {
        const card = entry.card;
        setNotice(entry, 'Loading…');
        try {
            const resp = await fetch(fileUrl(card, false), { credentials: 'same-origin' });
            if (!resp.ok) {
                setNotice(entry, resp.status === 404
                    ? 'This document is no longer available to you.'
                    : 'The document could not be loaded.', true);
                return;
            }
            const bytes = await resp.arrayBuffer();
            const message = {
                type: 'spark-preview-render',
                kind: card.kind,
                name: card.name,
                mime: card.type,
                size: card.size,
                theme: document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light',
                bytes: bytes,
            };
            if (card.kind === 'pdf') message.pdf = await pdfRenderer();
            await entry.ready;
            entry.frame.contentWindow.postMessage(message, '*', [bytes]);
            setNotice(entry, '');
        } catch (err) {
            setNotice(entry, 'The document could not be loaded.', true);
        }
    }

    function makeEntry(card) {
        const id = `preview-${card.conversation_id}-${card.file_id}`;
        const tabWrap = document.createElement('div');
        tabWrap.className = 'preview-tab-wrap';
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'preview-tab';
        tab.id = `${id}-tab`;
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-controls', `${id}-panel`);
        tab.title = card.title;
        tab.innerHTML = `<i class="bi ${KIND_ICONS[card.kind] || KIND_ICONS.other}" aria-hidden="true"></i><span>${esc(card.title)}</span>`;
        const tabClose = document.createElement('button');
        tabClose.type = 'button';
        tabClose.className = 'preview-tab-close';
        tabClose.tabIndex = -1;
        tabClose.setAttribute('aria-label', `Close ${card.title}`);
        tabClose.innerHTML = '<i class="bi bi-x" aria-hidden="true"></i>';
        tabWrap.appendChild(tab);
        tabWrap.appendChild(tabClose);

        const panel = document.createElement('section');
        panel.className = 'preview-panel';
        panel.id = `${id}-panel`;
        panel.setAttribute('role', 'tabpanel');
        panel.setAttribute('aria-labelledby', tab.id);
        panel.innerHTML = `
            <div class="preview-toolbar">
                <div class="preview-toolbar-text">
                    <div class="preview-toolbar-name">${esc(card.name)}</div>
                    <div class="preview-toolbar-meta">${esc(KIND_LABELS[card.kind] || 'File')} · ${esc(card.type || 'unknown type')} · ${esc(formatSize(card.size))}</div>
                </div>
                <a class="btn btn-app-ghost btn-sm preview-download" href="${esc(fileUrl(card, true))}" download aria-label="Download ${esc(card.name)}">
                    <i class="bi bi-download" aria-hidden="true"></i><span>Download</span>
                </a>
                <button type="button" class="btn btn-app-ghost btn-sm preview-close" aria-label="Close ${esc(card.name)}">
                    <i class="bi bi-x-lg" aria-hidden="true"></i><span>Close</span>
                </button>
            </div>
            <div class="preview-notice" role="status" hidden></div>`;
        const frame = document.createElement('iframe');
        frame.className = 'preview-frame';
        frame.setAttribute('sandbox', 'allow-scripts');
        frame.setAttribute('referrerpolicy', 'no-referrer');
        frame.title = `Preview of ${card.name}`;
        panel.appendChild(frame);

        const entry = { card, tab, tabWrap, panel, frame, notice: panel.querySelector('.preview-notice') };
        entry.ready = new Promise(resolve => { entry.resolveReady = resolve; });
        frame.src = '/preview/frame';

        tab.addEventListener('click', () => select(entry, false));
        tab.addEventListener('keydown', event => onTabKey(event, entry));
        tabClose.addEventListener('click', () => closeTab(entry));
        panel.querySelector('.preview-close').addEventListener('click', () => closeTab(entry));
        return entry;
    }

    function onTabKey(event, entry) {
        const index = state.tabs.indexOf(entry);
        let next = null;
        if (event.key === 'ArrowRight') next = (index + 1) % state.tabs.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + state.tabs.length) % state.tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = state.tabs.length - 1;
        else if (event.key === 'Delete') {
            event.preventDefault();
            closeTab(entry);
            return;
        }
        if (next === null) return;
        event.preventDefault();
        select(state.tabs[next], true);
    }

    /** Open a card's document in the pane (selecting its tab when it is already open). */
    function open(card, options) {
        const opts = options || {};
        if (!card || card.conversation_id !== pageConversation()) return;
        state.known.add(card.file_id);
        if (opts.from) state.returnFocus = opts.from;
        showPane();
        let entry = state.tabs.find(e => e.card.file_id === card.file_id);
        if (!entry) {
            if (state.tabs.length >= MAX_TABS) closeTab(state.tabs[0]);
            entry = makeEntry(card);
            state.tabs.push(entry);
            $('preview-tabs').appendChild(entry.tabWrap);
            $('preview-panels').appendChild(entry.panel);
            load(entry);
        }
        select(entry, !!opts.focus);
    }

    // -- Frames, resizing and keys ---------------------------------------------------

    window.addEventListener('message', event => {
        const entry = state.tabs.find(e => e.frame.contentWindow === event.source);
        if (!entry) return;
        const data = event.data || {};
        if (data.type === 'spark-preview-ready') {
            entry.resolveReady();
        } else if (data.type === 'spark-preview-rendered') {
            entry.panel.dataset.rendered = String(data.kind || '');
            try { entry.panel.dataset.detail = JSON.stringify(data.detail || {}); } catch (err) { /* ignore */ }
        } else if (data.type === 'spark-preview-error') {
            entry.panel.dataset.rendered = 'error';
            setNotice(entry, 'The preview could not be shown. Use Download to open the file.', true);
        }
    });

    function startResize(event) {
        if (isPhone()) return;
        event.preventDefault();
        const el = pane();
        const startX = event.clientX;
        const startWidth = el.getBoundingClientRect().width;
        el.classList.add('preview-resizing');
        const move = e => applyWidth(startWidth + (startX - e.clientX));
        const up = () => {
            el.classList.remove('preview-resizing');
            saveWidth(el.getBoundingClientRect().width);
            window.removeEventListener('pointermove', move);
            window.removeEventListener('pointerup', up);
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
    }

    function keyResize(event) {
        const step = event.shiftKey ? 96 : 32;
        const el = pane();
        const width = el.getBoundingClientRect().width;
        let value = null;
        if (event.key === 'ArrowLeft') value = applyWidth(width + step);
        else if (event.key === 'ArrowRight') value = applyWidth(width - step);
        if (value !== null) {
            event.preventDefault();
            saveWidth(value);
        }
    }

    function init() {
        const el = pane();
        if (!el) return;
        $('preview-pane-close').addEventListener('click', closeAll);
        const handle = $('preview-resize');
        handle.addEventListener('pointerdown', startResize);
        handle.addEventListener('keydown', keyResize);
        el.addEventListener('keydown', event => {
            if (event.key === 'Escape' && isPhone()) {
                event.preventDefault();
                closeAll();
            }
        });
        if (PHONE && PHONE.addEventListener) {
            PHONE.addEventListener('change', () => { if (!el.hidden) showPane(); });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    window.SparkPreview = {
        MARKER,
        parse,
        appendCard,
        beginHistory,
        endHistory,
        open,
        closeAll,
        _state: state,
    };
}());
