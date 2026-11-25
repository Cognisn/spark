/**
 * Spark — Main application JavaScript
 * Markdown rendering, utilities, and API helpers
 */


/* ==========================================================================
   1. Markdown & Code Highlighting
   ========================================================================== */

// Mermaid initialisation (lazy — only if mermaid.js is loaded). Strict: labels are
// sanitised by mermaid and diagrams cannot bind click handlers. With diagrams.js loaded
// its one configuration applies (HTML labels off, settings a diagram cannot change).
function initMermaid() {
    if (typeof mermaid === 'undefined') return;
    const dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    if (window.SparkDiagrams) {
        mermaid.initialize(SparkDiagrams.config(dark ? 'dark' : 'light'));
        return;
    }
    mermaid.initialize({
        startOnLoad: false,
        theme: dark ? 'dark' : 'default',
        securityLevel: 'strict',
    });
}

// Re-init mermaid on theme change
document.addEventListener('app-theme-changed', () => initMermaid());
document.addEventListener('DOMContentLoaded', () => initMermaid());


/**
 * Render markdown string to sanitised HTML, through the app's one renderer
 * (static/js/markdown.js, loaded by base.html). The code blocks' copy buttons are
 * answered by its delegated listener.
 */
function renderMarkdown(text) {
    if (!window.SparkMarkdown) return escapeHtml(text);
    return SparkMarkdown.render(text);
}


/**
 * Render a reply that is still streaming into an element, sanitised on every call;
 * blocks that have not changed since the last call keep their nodes.
 */
function renderMarkdownInto(el, text) {
    if (!window.SparkMarkdown) {
        el.textContent = text;
        return;
    }
    SparkMarkdown.renderInto(el, text);
}


/**
 * Apply syntax highlighting to code blocks within an element.
 */
function highlightCodeBlocks(container) {
    if (typeof hljs === 'undefined') return;
    // A block kept from an earlier render of a streaming reply is already highlighted.
    container.querySelectorAll('pre code:not([data-highlighted]):not(.hljs)').forEach(block => {
        hljs.highlightElement(block);
    });
}


/**
 * Render mermaid diagrams within an element.
 */
async function renderMermaidDiagrams(container) {
    if (typeof mermaid === 'undefined') return;
    const diagrams = container.querySelectorAll('.mermaid:not([data-processed])');
    for (const el of diagrams) {
        try {
            const id = 'mermaid-' + Math.random().toString(36).substr(2, 9);
            const { svg } = await mermaid.render(id, el.textContent);
            el.innerHTML = svg;
            el.setAttribute('data-processed', 'true');
        } catch (e) {
            el.innerHTML = `<pre style="color:var(--app-danger);">Diagram error: ${escapeHtml(e.message)}</pre>`;
        }
    }
}


/* ==========================================================================
   2. Utility Functions
   ========================================================================== */

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatTimestamp(ts) {
    const d = new Date(ts);
    return d.toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
    });
}

function formatNumber(n) {
    return new Intl.NumberFormat().format(n);
}

function truncateText(text, maxLength = 100) {
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function debounce(fn, ms = 250) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}


/* ==========================================================================
   3. API Request Helper
   ========================================================================== */

async function apiRequest(url, options = {}) {
    const defaults = {
        headers: { 'Content-Type': 'application/json' },
    };
    const config = { ...defaults, ...options };
    if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
        config.body = JSON.stringify(options.body);
    }
    try {
        const response = await fetch(url, config);
        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }
        return response.json();
    } catch (err) {
        AppToast.danger('Request failed', err.message);
        throw err;
    }
}


/* ==========================================================================
   4. Download Helper
   ========================================================================== */

function downloadFile(content, filename, mimeType = 'text/plain') {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
