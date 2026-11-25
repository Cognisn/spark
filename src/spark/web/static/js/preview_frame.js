/**
 * Spark: the document preview's renderer (CS38), inside the sandboxed frame.
 *
 * The frame runs in an opaque origin (sandbox="allow-scripts") under a Content
 * Security Policy that allows SPARK's own scripts and no network request. The pane
 * posts the document's bytes here; each kind has one renderer:
 *
 *   pdf       PDF.js (its module and worker handed over by the pane as text, loaded
 *             from blob: URLs; the worker runs on this thread, eval is off)
 *   docx      docx-preview with JSZip
 *   sheet     SheetJS Community Edition: read-only tables with sheet tabs and a row cap
 *   image     an <img> from a blob: URL (SVG too: an image never runs its scripts)
 *   markdown  marked, then DOMPurify
 *   text      text and code with line numbers
 *   other     name, type and size: the pane offers Download
 *
 * Every document is untrusted: nothing it holds becomes markup except through
 * DOMPurify (Markdown) or the Word renderer (which builds elements, never scripts),
 * and a link inside it never navigates the frame.
 */
(function () {
    'use strict';

    const ROW_CAP = 1000;
    const COLUMN_CAP = 100;
    const LINE_CAP = 20000;
    const PAGE_CAP = 300;
    const VENDOR = '/static/vendor';

    const root = document.getElementById('preview-root');
    const loaded = {};

    function post(message) {
        // The parent is SPARK's chat page; nothing private is ever sent.
        window.parent.postMessage(message, '*');
    }

    function clear() {
        while (root.firstChild) root.removeChild(root.firstChild);
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function formatSize(bytes) {
        const n = Number(bytes) || 0;
        if (n < 1024) return `${n.toLocaleString()} bytes`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    }

    function status(text, className) {
        clear();
        root.appendChild(element('p', className || 'preview-status', text));
    }

    function loadScript(name) {
        if (!loaded[name]) {
            loaded[name] = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = `${VENDOR}/${name}`;
                script.onload = () => resolve();
                script.onerror = () => reject(new Error(`could not load ${name}`));
                document.head.appendChild(script);
            });
        }
        return loaded[name];
    }

    function decodeText(bytes) {
        return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    }

    // -- Renderers ----------------------------------------------------------------

    async function renderPdf(doc) {
        if (!doc.pdf || !doc.pdf.lib || !doc.pdf.worker) throw new Error('the PDF renderer is missing');
        const workerUrl = URL.createObjectURL(new Blob([doc.pdf.worker], { type: 'text/javascript' }));
        const libUrl = URL.createObjectURL(new Blob([doc.pdf.lib], { type: 'text/javascript' }));
        // Importing the worker module sets globalThis.pdfjsWorker, so PDF.js runs its
        // worker on this thread (an opaque origin cannot start a worker of its own).
        await import(workerUrl);
        const pdfjs = await import(libUrl);
        pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
        const pdf = await pdfjs.getDocument({
            data: new Uint8Array(doc.bytes),
            isEvalSupported: false,
            enableXfa: false,
            useSystemFonts: true,
        }).promise;
        clear();
        const pages = element('div', 'preview-pdf');
        root.appendChild(pages);
        const total = Math.min(pdf.numPages, PAGE_CAP);
        const width = Math.max(root.clientWidth - 32, 200);
        const ratio = window.devicePixelRatio || 1;
        for (let number = 1; number <= total; number += 1) {
            const page = await pdf.getPage(number);
            const base = page.getViewport({ scale: 1 });
            const scale = width / base.width;
            const viewport = page.getViewport({ scale: scale * ratio });
            const canvas = element('canvas', 'preview-pdf-page');
            canvas.width = Math.floor(viewport.width);
            canvas.height = Math.floor(viewport.height);
            canvas.style.width = `${Math.floor(viewport.width / ratio)}px`;
            canvas.setAttribute('role', 'img');
            canvas.setAttribute('aria-label', `Page ${number} of ${pdf.numPages}`);
            pages.appendChild(canvas);
            await page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise;
        }
        if (pdf.numPages > total) {
            pages.appendChild(element('p', 'preview-note',
                `Showing the first ${total} of ${pdf.numPages} pages. Download the file to see the rest.`));
        }
        return { pages: pdf.numPages, shown: total };
    }

    async function renderDocx(doc) {
        await loadScript('jszip/jszip.min.js');
        await loadScript('docx-preview/docx-preview.min.js');
        clear();
        const container = element('div', 'preview-docx');
        root.appendChild(container);
        await window.docx.renderAsync(doc.bytes, container, null, {
            inWrapper: true,
            breakPages: true,
            ignoreLastRenderedPageBreak: true,
            experimental: false,
            useBase64URL: true,
            renderChanges: false,
        });
        // Pages are drawn at their paper width: scale them down to fit the pane.
        const page = container.querySelector('section.docx');
        const wrapper = container.querySelector('.docx-wrapper');
        if (page && wrapper) {
            const room = root.clientWidth - 32;
            const needed = page.offsetWidth + 32;
            if (room > 0 && needed > room) wrapper.style.zoom = String(room / needed);
        }
        return { paragraphs: container.querySelectorAll('p').length, tables: container.querySelectorAll('table').length };
    }

    function sheetTable(XLSX, sheet) {
        const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: '', blankrows: true });
        const table = element('table', 'preview-sheet-table');
        const body = element('tbody');
        table.appendChild(body);
        const shown = rows.slice(0, ROW_CAP);
        shown.forEach((row, index) => {
            const tr = element('tr');
            tr.appendChild(element('th', 'preview-row-number', String(index + 1)));
            (row || []).slice(0, COLUMN_CAP).forEach(cell => {
                tr.appendChild(element('td', null, cell === null || cell === undefined ? '' : String(cell)));
            });
            body.appendChild(tr);
        });
        return { table: table, total: rows.length, shown: shown.length };
    }

    async function renderSheet(doc) {
        await loadScript('sheetjs/xlsx.full.min.js');
        const XLSX = window.XLSX;
        const book = XLSX.read(new Uint8Array(doc.bytes), {
            type: 'array',
            dense: true,
            cellHTML: false,
            cellFormula: false,
            sheetRows: ROW_CAP + 1,
        });
        clear();
        const tabs = element('div', 'preview-sheet-tabs');
        tabs.setAttribute('role', 'tablist');
        tabs.setAttribute('aria-label', 'Sheets');
        const panel = element('div', 'preview-sheet-panel');
        panel.setAttribute('role', 'tabpanel');
        panel.tabIndex = 0;
        root.appendChild(tabs);
        root.appendChild(panel);
        const buttons = [];

        function show(index) {
            const name = book.SheetNames[index];
            buttons.forEach((button, i) => {
                button.setAttribute('aria-selected', i === index ? 'true' : 'false');
                button.tabIndex = i === index ? 0 : -1;
            });
            while (panel.firstChild) panel.removeChild(panel.firstChild);
            const drawn = sheetTable(XLSX, book.Sheets[name]);
            panel.setAttribute('aria-label', name);
            panel.appendChild(drawn.table);
            if (drawn.total > drawn.shown) {
                panel.appendChild(element('p', 'preview-note',
                    `Showing the first ${drawn.shown} rows. Download the file to see the rest.`));
            }
        }

        book.SheetNames.forEach((name, index) => {
            const button = element('button', 'preview-sheet-tab', name);
            button.type = 'button';
            button.setAttribute('role', 'tab');
            button.addEventListener('click', () => show(index));
            button.addEventListener('keydown', event => {
                if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
                event.preventDefault();
                const step = event.key === 'ArrowRight' ? 1 : -1;
                const next = (index + step + buttons.length) % buttons.length;
                show(next);
                buttons[next].focus();
            });
            buttons.push(button);
            tabs.appendChild(button);
        });
        if (buttons.length < 2) tabs.hidden = true;
        if (buttons.length) show(0);
        return { sheets: book.SheetNames.length, cells: panel.querySelectorAll('td').length };
    }

    function renderImage(doc) {
        clear();
        const url = URL.createObjectURL(new Blob([doc.bytes], { type: doc.mime || 'application/octet-stream' }));
        const image = element('img', 'preview-image');
        image.alt = doc.name || 'Image';
        root.appendChild(image);
        return new Promise((resolve, reject) => {
            image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
            image.onerror = () => reject(new Error('the image could not be shown'));
            image.src = url;
        });
    }

    function renderMarkdown(doc) {
        const html = window.marked.parse(decodeText(doc.bytes), { gfm: true, breaks: false, async: false });
        const clean = window.DOMPurify.sanitize(html, {
            USE_PROFILES: { html: true },
            FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'object', 'embed'],
            FORBID_ATTR: ['style'],
        });
        clear();
        const container = element('article', 'preview-markdown');
        container.innerHTML = clean;
        root.appendChild(container);
        return { headings: container.querySelectorAll('h1, h2, h3').length, scripts: container.querySelectorAll('script, [onerror], [onload]').length };
    }

    function renderText(doc) {
        const lines = decodeText(doc.bytes).replace(/\r\n?/g, '\n').split('\n');
        clear();
        const table = element('table', 'preview-code');
        const body = element('tbody');
        table.appendChild(body);
        const shown = lines.slice(0, LINE_CAP);
        shown.forEach((line, index) => {
            const tr = element('tr');
            const number = element('td', 'preview-line-number', String(index + 1));
            number.setAttribute('aria-hidden', 'true');
            tr.appendChild(number);
            tr.appendChild(element('td', 'preview-line', line));
            body.appendChild(tr);
        });
        root.appendChild(table);
        if (lines.length > shown.length) {
            root.appendChild(element('p', 'preview-note',
                `Showing the first ${shown.length.toLocaleString()} lines. Download the file to see the rest.`));
        }
        return { lines: shown.length };
    }

    function renderOther(doc) {
        clear();
        const box = element('div', 'preview-details');
        box.appendChild(element('p', 'preview-details-name', doc.name || 'Document'));
        const list = element('dl');
        [['Type', doc.mime || 'unknown'], ['Size', formatSize(doc.size)]].forEach(([term, value]) => {
            list.appendChild(element('dt', null, term));
            list.appendChild(element('dd', null, value));
        });
        box.appendChild(list);
        box.appendChild(element('p', null, 'This type of file cannot be shown here. Use Download to open it.'));
        root.appendChild(box);
        return {};
    }

    const RENDERERS = {
        pdf: renderPdf,
        docx: renderDocx,
        sheet: renderSheet,
        image: renderImage,
        markdown: renderMarkdown,
        text: renderText,
        other: renderOther,
    };

    async function render(doc) {
        const renderer = RENDERERS[doc.kind] || renderOther;
        // Pages (PDF, Word, sheets, images) stay on paper; text follows the app's theme.
        const themed = doc.theme === 'dark' && ['markdown', 'text', 'other'].includes(doc.kind);
        document.body.dataset.theme = themed ? 'dark' : 'light';
        status('Loading the preview…');
        try {
            const detail = (await renderer(doc)) || {};
            document.documentElement.dataset.rendered = doc.kind in RENDERERS ? doc.kind : 'other';
            post({ type: 'spark-preview-rendered', kind: doc.kind, detail: detail });
        } catch (err) {
            renderOther(doc);
            root.insertBefore(element('p', 'preview-error', 'The preview could not be shown.'), root.firstChild);
            document.documentElement.dataset.rendered = 'error';
            post({ type: 'spark-preview-error', kind: doc.kind });
        }
    }

    // A link in a document never navigates the frame (it would leave this policy).
    document.addEventListener('click', event => {
        const link = event.target && event.target.closest ? event.target.closest('a[href]') : null;
        if (link && !(link.getAttribute('href') || '').startsWith('#')) event.preventDefault();
    }, true);

    window.addEventListener('message', event => {
        if (event.source !== window.parent) return;
        const data = event.data || {};
        if (data.type !== 'spark-preview-render' || !(data.bytes instanceof ArrayBuffer)) return;
        render(data);
    });

    post({ type: 'spark-preview-ready' });
}());
