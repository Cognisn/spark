/**
 * Spark — Main application JavaScript
 * Markdown rendering, utilities, and API helpers
 */


/* ==========================================================================
   1. Markdown & Code Highlighting
   ========================================================================== */

// Mermaid initialisation (lazy — only if mermaid.js is loaded)
function initMermaid() {
    if (typeof mermaid === 'undefined') return;
    const theme = document.documentElement.getAttribute('data-bs-theme') === 'dark'
        ? 'dark' : 'default';
    mermaid.initialize({
        startOnLoad: false,
        theme: theme,
        securityLevel: 'loose',
    });
}

// Re-init mermaid on theme change
document.addEventListener('app-theme-changed', () => initMermaid());
document.addEventListener('DOMContentLoaded', () => initMermaid());


// Marked.js configuration (lazy — only if marked is loaded)
function configureMarked() {
    if (typeof marked === 'undefined') return;

    const renderer = new marked.Renderer();

    // Code blocks — add copy button and mermaid support
    // Marked v12+ passes a single token object {text, lang, escaped}
    renderer.code = function (token) {
        // Handle both old (code, language) and new ({text, lang}) signatures
        let code, language;
        if (typeof token === 'object' && token !== null && 'text' in token) {
            code = token.text;
            language = token.lang || '';
        } else {
            code = token;
            language = arguments[1] || '';
        }

        if (language === 'mermaid') {
            return `<div class="mermaid">${code}</div>`;
        }
        const escaped = escapeHtml(code);
        const langClass = language ? ` class="language-${language}"` : '';
        return `<div class="code-block-wrapper">
            <div class="code-header">
                <span class="code-language">${language || 'text'}</span>
                <button class="btn btn-app-ghost btn-sm copy-btn" onclick="copyToClipboard(this)" title="Copy">
                    <i class="bi bi-clipboard"></i>
                </button>
            </div>
            <pre><code${langClass}>${escaped}</code></pre>
        </div>`;
    };

    marked.setOptions({
        renderer: renderer,
        gfm: true,
        breaks: true,
    });
}

document.addEventListener('DOMContentLoaded', () => configureMarked());


/**
 * Render markdown string to HTML.
 */
function renderMarkdown(text) {
    if (typeof marked === 'undefined') return escapeHtml(text);
    return marked.parse(text);
}


/**
 * Apply syntax highlighting to code blocks within an element.
 */
function highlightCodeBlocks(container) {
    if (typeof hljs === 'undefined') return;
    container.querySelectorAll('pre code').forEach(block => {
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

function copyToClipboard(btn) {
    const code = btn.closest('.code-block-wrapper').querySelector('code');
    navigator.clipboard.writeText(code.textContent).then(() => {
        const icon = btn.querySelector('i');
        icon.classList.replace('bi-clipboard', 'bi-check-lg');
        setTimeout(() => icon.classList.replace('bi-check-lg', 'bi-clipboard'), 2000);
    });
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
