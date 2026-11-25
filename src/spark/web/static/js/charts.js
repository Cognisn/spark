/**
 * Spark: charts and diagrams in the conversation (PC20).
 *
 * create_chart and create_diagram answer with a card line (the document preview's
 * "[spark-preview]" line, kind "chart" or "diagram"). SparkCharts.appendCard draws the
 * card in the message: its title, the chart or diagram at a fixed height (tooltips and
 * legend toggles), its caption, and a toolbar: Open in preview, Save as PNG, Save as SVG
 * and, for a chart, Download data (CSV), with Match theme for saved images (white is
 * the default). The preview pane uses the same toolbar (SparkCharts.toolbar).
 *
 * What is drawn always comes from the chart API: GET /charts/api/<file id> answers the
 * ECharts option SPARK built (data only) or the diagram's Mermaid source, through the
 * conversation's access decision. Nothing the model wrote in the page is ever drawn.
 * ECharts loads from SPARK (static/vendor) the first time a chart is drawn.
 *
 * The logic part (no DOM, no ECharts) exports itself under Node for tests.
 */
(function (root) {
    'use strict';

    const HEIGHT = 320;
    const EXPORT_SIZE = { width: 1200, height: 720 };
    const PIXEL_RATIO = 2;
    const ECHARTS = '/static/vendor/echarts/echarts.min.js';
    const KINDS = ['chart', 'diagram'];

    // -- Logic ----------------------------------------------------------------------

    /** A file name from a title: no path or reserved characters, never empty. */
    function fileName(title, extension) {
        const base = String(title || '')
            .replace(/[\u0000-\u001f\u007f\\/:*?"<>|＜＞]+/g, ' ')
            .replace(/\s+/g, ' ')
            .trim()
            .replace(/^[.\s]+|[.\s]+$/g, '')
            .slice(0, 100)
            .trim();
        return `${base || 'chart'}.${extension}`;
    }

    function viewUrl(fileId, theme, forExport) {
        const params = new URLSearchParams({ theme: theme === 'dark' ? 'dark' : 'light' });
        if (forExport) params.set('export', '1');
        return `/charts/api/${encodeURIComponent(fileId)}?${params}`;
    }

    function dataUrl(fileId) {
        return `/charts/api/${encodeURIComponent(fileId)}/data.csv`;
    }

    /** The toolbar's actions for a kind; the pane has no Open in preview. */
    function actions(kind, inPane) {
        const all = ['open', 'png', 'svg', 'csv'];
        return all.filter(a => !(inPane && a === 'open') && !(kind !== 'chart' && a === 'csv'));
    }

    /** The theme a saved image takes: the page's with Match theme, else white (light). */
    function exportTheme(matchTheme, pageTheme) {
        return matchTheme && pageTheme === 'dark' ? 'dark' : 'light';
    }

    function isArtefact(card) {
        return !!card && KINDS.includes(card.kind);
    }

    const logic = { HEIGHT, EXPORT_SIZE, PIXEL_RATIO, fileName, viewUrl, dataUrl, actions,
        exportTheme, isArtefact };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = logic;
        return;
    }

    // -- The page ---------------------------------------------------------------------

    const LABELS = {
        open: ['Open in preview', 'bi-layout-sidebar-reverse'],
        png: ['Save as PNG', 'bi-file-earmark-image'],
        svg: ['Save as SVG', 'bi-filetype-svg'],
        csv: ['Download data', 'bi-filetype-csv'],
    };
    const drawn = new Set();  // {card, canvas, chart, status}
    let echartsLoading = null;

    function pageTheme() {
        return document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
    }

    function loadECharts() {
        if (root.echarts) return Promise.resolve(root.echarts);
        if (!echartsLoading) {
            echartsLoading = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = ECHARTS;
                script.onload = () => resolve(root.echarts);
                script.onerror = () => {
                    echartsLoading = null;
                    reject(new Error('the chart library could not be loaded'));
                };
                document.head.appendChild(script);
            });
        }
        return echartsLoading;
    }

    async function fetchPayload(card, theme, forExport) {
        const resp = await fetch(viewUrl(card.file_id, theme, forExport), { credentials: 'same-origin' });
        if (!resp.ok) {
            const error = new Error(resp.status === 404
                ? 'This chart is no longer available to you.'
                : 'The chart could not be loaded.');
            error.status = resp.status;
            throw error;
        }
        return resp.json();
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function download(href, name) {
        const link = document.createElement('a');
        link.href = href;
        link.download = name;
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        link.remove();
    }

    function downloadBlob(blob, name) {
        const url = URL.createObjectURL(blob);
        download(url, name);
        setTimeout(() => URL.revokeObjectURL(url), 10000);
    }

    /** A chart drawn off screen at a fixed size, for a saved image. */
    async function offscreenChart(payload, renderer) {
        const echarts = await loadECharts();
        const holder = element('div', 'chart-export-holder');
        holder.style.width = `${EXPORT_SIZE.width}px`;
        holder.style.height = `${EXPORT_SIZE.height}px`;
        document.body.appendChild(holder);
        const chart = echarts.init(holder, null, { renderer, ...EXPORT_SIZE });
        chart.setOption(payload.option);
        return { chart, done: () => { chart.dispose(); holder.remove(); } };
    }

    async function diagramSvg(payload) {
        const holder = element('div', 'chart-export-holder');
        document.body.appendChild(holder);
        try {
            const svg = await root.SparkDiagrams.render(holder, payload.source, payload.theme);
            return root.SparkDiagrams.exportSvg(svg, payload);
        } finally {
            holder.remove();
        }
    }

    function svgToPng(svgText) {
        return new Promise((resolve, reject) => {
            const url = URL.createObjectURL(new Blob([svgText], { type: 'image/svg+xml' }));
            const image = new Image();
            image.onload = () => {
                const canvas = document.createElement('canvas');
                canvas.width = Math.ceil(image.width * PIXEL_RATIO);
                canvas.height = Math.ceil(image.height * PIXEL_RATIO);
                const context = canvas.getContext('2d');
                context.scale(PIXEL_RATIO, PIXEL_RATIO);
                context.drawImage(image, 0, 0);
                URL.revokeObjectURL(url);
                canvas.toBlob(blob => (blob ? resolve(blob) : reject(new Error('no image'))), 'image/png');
            };
            image.onerror = () => {
                URL.revokeObjectURL(url);
                reject(new Error('the diagram could not be drawn as an image'));
            };
            image.src = url;
        });
    }

    /** Save a card's chart or diagram as an image (png or svg), titled, on white by default. */
    async function saveImage(card, format, matchTheme) {
        const theme = exportTheme(matchTheme, pageTheme());
        const payload = await fetchPayload(card, theme, true);
        const name = fileName(payload.title || card.title, format);
        if (payload.kind === 'diagram') {
            const svg = await diagramSvg(payload);
            if (format === 'svg') downloadBlob(new Blob([svg], { type: 'image/svg+xml' }), name);
            else downloadBlob(await svgToPng(svg), name);
            return name;
        }
        const off = await offscreenChart(payload, format === 'svg' ? 'svg' : 'canvas');
        try {
            if (format === 'svg') {
                downloadBlob(new Blob([off.chart.renderToSVGString()], { type: 'image/svg+xml' }), name);
            } else {
                download(off.chart.getDataURL({
                    type: 'png', pixelRatio: PIXEL_RATIO, backgroundColor: payload.background,
                }), name);
            }
        } finally {
            off.done();
        }
        return name;
    }

    function setStatus(status, text, isError) {
        status.textContent = text || '';
        status.hidden = !text;
        status.classList.toggle('chart-card-error', !!isError);
    }

    /**
     * The toolbar for a card: in the message (with Open in preview) or in the pane.
     * Buttons are native, so the keyboard reaches each; the arrow keys move between them.
     */
    function toolbar(card, options) {
        const opts = options || {};
        const bar = element('div', 'chart-toolbar');
        bar.setAttribute('role', 'toolbar');
        bar.setAttribute('aria-label', `${card.kind === 'chart' ? 'Chart' : 'Diagram'} actions for ${card.title}`);
        const status = opts.status || element('span', 'chart-toolbar-status');
        const match = element('input');
        match.type = 'checkbox';
        match.className = 'form-check-input chart-match-input';
        actions(card.kind, !!opts.inPane).forEach(action => {
            const [label, icon] = LABELS[action];
            let control;
            if (action === 'csv') {
                control = element('a', 'btn btn-app-ghost btn-sm chart-action');
                control.href = dataUrl(card.file_id);
                control.setAttribute('download', fileName(card.title, 'csv'));
            } else {
                control = element('button', 'btn btn-app-ghost btn-sm chart-action');
                control.type = 'button';
            }
            control.dataset.action = action;
            control.setAttribute('aria-label', `${label}: ${card.title}`);
            const glyph = element('i', `bi ${icon}`);
            glyph.setAttribute('aria-hidden', 'true');
            control.appendChild(glyph);
            control.appendChild(element('span', null, label));
            if (action === 'open') {
                control.addEventListener('click', () => {
                    if (root.SparkPreview) root.SparkPreview.open(card, { focus: true, from: control });
                });
            } else if (action !== 'csv') {
                control.addEventListener('click', async () => {
                    control.disabled = true;
                    try {
                        const name = await saveImage(card, action, match.checked);
                        setStatus(status, `Saved ${name}`);
                    } catch (err) {
                        setStatus(status, err && err.message ? err.message : 'It could not be saved.', true);
                    } finally {
                        control.disabled = false;
                    }
                });
            }
            bar.appendChild(control);
        });
        const label = element('label', 'chart-match');
        label.appendChild(match);
        label.appendChild(element('span', null, 'Match theme'));
        label.title = 'Save images in the current theme instead of on white';
        bar.appendChild(label);
        if (!opts.status) bar.appendChild(status);
        bar.addEventListener('keydown', event => {
            if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
            const items = [...bar.querySelectorAll('.chart-action, .chart-match-input')];
            const index = items.indexOf(document.activeElement);
            if (index < 0) return;
            event.preventDefault();
            const step = event.key === 'ArrowRight' ? 1 : -1;
            items[(index + step + items.length) % items.length].focus();
        });
        return bar;
    }

    async function draw(entry) {
        const theme = pageTheme();
        const { card, canvas, status } = entry;
        try {
            const payload = await fetchPayload(card, theme, false);
            if (entry.caption) {
                entry.caption.textContent = payload.caption || '';
                entry.caption.hidden = !payload.caption;
            }
            if (payload.kind === 'diagram') {
                const svg = await root.SparkDiagrams.render(canvas, payload.source, theme);
                if (svg) {
                    // Fit the whole diagram in the card's fixed height (Mermaid sizes it
                    // to its natural width); the pane shows it full size.
                    svg.style.maxWidth = '100%';
                    svg.style.width = '100%';
                    svg.style.height = '100%';
                }
            } else {
                const echarts = await loadECharts();
                if (!entry.chart) {
                    entry.chart = echarts.init(canvas, null, { renderer: 'canvas' });
                    if (root.ResizeObserver) {
                        entry.observer = new ResizeObserver(() => entry.chart && entry.chart.resize());
                        entry.observer.observe(canvas);
                    }
                }
                entry.chart.setOption(payload.option, true);
            }
            canvas.dataset.drawn = payload.kind;
            setStatus(status, '');
        } catch (err) {
            canvas.dataset.drawn = 'error';
            setStatus(status, err && err.message ? err.message : 'It could not be drawn.', true);
        }
    }

    /**
     * Draw a chart or diagram card at the end of the conversation. Returns the card's
     * element, or null when the card is not an artefact of this conversation.
     */
    function appendCard(card) {
        const container = document.getElementById('chat-messages');
        const pane = document.getElementById('preview-pane');
        const conversation = pane ? Number(pane.dataset.conversationId) : NaN;
        if (!isArtefact(card) || !container || card.conversation_id !== conversation) return null;
        const wrapper = element('div', 'chat-message-wrapper assistant chart-card-wrapper');
        const figure = element('figure', 'chart-card');
        figure.dataset.fileId = String(card.file_id);
        figure.dataset.kind = card.kind;
        const header = element('div', 'chart-card-header');
        const icon = element('i', `bi ${card.kind === 'chart' ? 'bi-bar-chart-line' : 'bi-diagram-3'} chart-card-icon`);
        icon.setAttribute('aria-hidden', 'true');
        header.appendChild(icon);
        header.appendChild(element('span', 'chart-card-title', card.title));
        const status = element('span', 'chart-toolbar-status');
        status.setAttribute('role', 'status');
        status.hidden = true;
        figure.appendChild(header);
        figure.appendChild(toolbar(card, { status }));
        const canvas = element('div', 'chart-card-canvas');
        canvas.style.height = `${HEIGHT}px`;
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label', `${card.kind === 'chart' ? 'Chart' : 'Diagram'}: ${card.title}`);
        figure.appendChild(canvas);
        const caption = element('figcaption', 'chart-card-caption');
        caption.hidden = true;
        figure.appendChild(caption);
        figure.appendChild(status);
        wrapper.appendChild(figure);
        container.appendChild(wrapper);
        const entry = { card, canvas, caption, status, chart: null };
        drawn.add(entry);
        draw(entry);
        return figure;
    }

    /** Draw a card's chart or diagram into the pane's frame message (used by preview.js). */
    async function framePayload(card) {
        const payload = await fetchPayload(card, pageTheme(), false);
        return new TextEncoder().encode(JSON.stringify(payload)).buffer;
    }

    function redrawAll() {
        drawn.forEach(entry => {
            if (!document.contains(entry.canvas)) {
                if (entry.chart) entry.chart.dispose();
                if (entry.observer) entry.observer.disconnect();
                drawn.delete(entry);
                return;
            }
            draw(entry);
        });
    }

    document.addEventListener('app-theme-changed', redrawAll);

    root.SparkCharts = {
        ...logic,
        appendCard,
        toolbar,
        saveImage,
        framePayload,
        redrawAll,
        _drawn: drawn,
    };
}(typeof window !== 'undefined' ? window : globalThis));
