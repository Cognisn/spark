/**
 * Spark: Mermaid diagrams (PC20), one configuration for every place a diagram is drawn.
 *
 * SparkDiagrams.config(theme) is the only Mermaid configuration SPARK uses: the strict
 * security level (labels sanitised, no click handlers), HTML labels off (labels are SVG
 * text, never markup), and the settings a diagram may not change listed as secure, so
 * a directive in its source cannot turn any of it back. main.js initialises Mermaid
 * with it for diagrams in ordinary replies; this script renders stored diagrams in the
 * message, the preview frame and saved images with it.
 *
 * Stored diagrams come from the chart API (/charts/api/<file id>), never from the
 * model's text in the page. The logic part has no DOM and exports itself under Node
 * for tests.
 */
(function (root) {
    'use strict';

    // The settings a diagram's own directives may never change.
    const SECURE = [
        'secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges',
        'suppressErrorRendering', 'htmlLabels', 'flowchart', 'theme', 'themeVariables',
        'themeCSS', 'fontFamily', 'dompurifyConfig', 'darkMode',
    ];

    function config(theme) {
        return {
            startOnLoad: false,
            securityLevel: 'strict',
            theme: theme === 'dark' ? 'dark' : 'default',
            htmlLabels: false,
            flowchart: { htmlLabels: false },
            secure: SECURE.slice(),
        };
    }

    let counter = 0;

    function nextId() {
        counter += 1;
        return `spark-diagram-${Date.now().toString(36)}-${counter}`;
    }

    /** Draw Mermaid source into container (its SVG only); resolves with the SVG element. */
    async function render(container, source, theme) {
        const mermaid = root.mermaid;
        if (!mermaid) throw new Error('Mermaid is not loaded');
        mermaid.initialize(config(theme));
        try {
            const result = await mermaid.render(nextId(), String(source || ''));
            container.innerHTML = result.svg;
        } finally {
            // Put the page's own setting back for diagrams in ordinary replies.
            const pageTheme = root.document && root.document.documentElement
                && root.document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
            mermaid.initialize(config(pageTheme));
        }
        const svg = container.querySelector('svg');
        fitViewBox(svg);
        return svg;
    }

    // Mermaid can measure its drawing short (the loop-back edges of a flowchart reach past
    // it): fit the view box to what was drawn, with a margin, once it is in the page.
    function fitViewBox(svg) {
        if (!svg || typeof svg.getBBox !== 'function') return;
        try {
            const box = svg.getBBox();
            if (!box.width || !box.height) return;
            const pad = 8;
            svg.setAttribute('viewBox',
                `${box.x - pad} ${box.y - pad} ${box.width + pad * 2} ${box.height + pad * 2}`);
        } catch (err) {
            // Not laid out (hidden): keep Mermaid's own view box.
        }
    }

    const SVG_NS = 'http://www.w3.org/2000/svg';

    /**
     * A standalone SVG of a drawn diagram for saving: the title (and caption) above it on
     * the given background, built as elements so no text is ever read as markup.
     */
    function exportSvg(drawn, details) {
        const doc = root.document;
        const box = drawn.viewBox && drawn.viewBox.baseVal && drawn.viewBox.baseVal.width
            ? drawn.viewBox.baseVal
            : drawn.getBBox();
        const pad = 24;
        const heading = details.title ? 40 : 0;
        const sub = details.caption ? 24 : 0;
        const width = Math.ceil(Math.max(box.width, 320) + pad * 2);
        const height = Math.ceil(box.height + pad * 2 + heading + sub);
        const out = doc.createElementNS(SVG_NS, 'svg');
        out.setAttribute('xmlns', SVG_NS);
        out.setAttribute('width', String(width));
        out.setAttribute('height', String(height));
        out.setAttribute('viewBox', `0 0 ${width} ${height}`);
        const back = doc.createElementNS(SVG_NS, 'rect');
        back.setAttribute('width', '100%');
        back.setAttribute('height', '100%');
        back.setAttribute('fill', details.background);
        out.appendChild(back);
        const texts = [[details.title, 18, pad + 22, '600'], [details.caption, 13, pad + heading + 14, '400']];
        texts.forEach(([text, size, y, weight]) => {
            if (!text) return;
            const node = doc.createElementNS(SVG_NS, 'text');
            node.setAttribute('x', String(width / 2));
            node.setAttribute('y', String(y));
            node.setAttribute('text-anchor', 'middle');
            node.setAttribute('font-family', 'DM Sans, Inter, Segoe UI, sans-serif');
            node.setAttribute('font-size', String(size));
            node.setAttribute('font-weight', weight);
            node.setAttribute('fill', details.text);
            node.textContent = text;
            out.appendChild(node);
        });
        const inner = drawn.cloneNode(true);
        inner.removeAttribute('style');
        inner.setAttribute('x', String((width - box.width) / 2));
        inner.setAttribute('y', String(pad + heading + sub));
        inner.setAttribute('width', String(box.width));
        inner.setAttribute('height', String(box.height));
        out.appendChild(inner);
        return new root.XMLSerializer().serializeToString(out);
    }

    // Mermaid takes this configuration as soon as both are loaded, so no diagram is ever
    // drawn with Mermaid's defaults (main.js sets it again when the theme changes).
    if (root.mermaid && typeof root.mermaid.initialize === 'function') {
        const dark = root.document && root.document.documentElement
            && root.document.documentElement.getAttribute('data-bs-theme') === 'dark';
        root.mermaid.initialize(config(dark ? 'dark' : 'light'));
    }

    const api = { SECURE, config, render, exportSvg };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.SparkDiagrams = api;
}(typeof window !== 'undefined' ? window : globalThis));
