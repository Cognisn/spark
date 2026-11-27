/**
 * Spark: the one Markdown renderer for SPARK's own pages.
 *
 * window.SparkMarkdown turns Markdown into HTML that is safe to insert into a page:
 *   - render(text) parses with marked (self-hosted under static/vendor) and passes the
 *     result through DOMPurify, every time, so no caller ever sees marked's raw output.
 *     Replies, live streams, debates, panels, action results, help and the release
 *     notes all come through here (main.js renderMarkdown is a thin wrapper);
 *   - renderInto(el, text) draws a reply that is still streaming into an element,
 *     sanitising each new or changed block on every call and keeping the nodes of the
 *     blocks that have not changed (main.js renderMarkdownInto);
 *   - safe(text) is the same renderer, kept under the name the release notes use;
 *   - sanitize(html) is the sanitiser on its own, with the same configuration.
 *
 * One DOMPurify configuration for the app: the HTML profile only (no SVG or MathML),
 * no style, form, frame, object or embed elements, no style, id or name attributes, no
 * data attributes other than the copy control's, only the classes SPARK's own renderer
 * writes, URLs limited to DOMPurify's safe schemes, and links to other pages opened in a
 * new tab without an opener or referrer.
 *
 * Images: a reply cannot make the reader's browser fetch from a host it names. An image
 * stays inline only when it comes from SPARK itself (the page's own origin) or is a
 * png, jpeg, gif or webp data URL. Any other image is replaced, when it is sanitised,
 * by text giving its alt text and its host. An https image is a button the reader
 * clicks (or presses Enter or Space on) to load that one image; nothing is fetched
 * before that, and an image over any other scheme (http, protocol-relative and the rest)
 * cannot be loaded at all. Audio, video, pictures and tracks are removed, as are
 * posters and background images.
 *
 * Rendered Markdown carries no inline handlers: the code block's copy button is marked
 * with data-spark-copy and answered by one delegated listener on the document.
 */
(function () {
    'use strict';

    const COPY_ATTR = 'data-spark-copy';
    // Marks the placeholder of a remote image the reader may choose to load. Written only
    // by the sanitiser's own hook (it is not in ADD_ATTR, so a reply cannot carry it).
    const IMAGE_ATTR = 'data-spark-image';
    const OWN_DATA_IMAGE = /^data:image\/(?:png|jpeg|gif|webp)[;,]/i;
    const PROTOCOL_RELATIVE = /^[\\/]{2}/;

    // Classes SPARK's renderer writes (and the language class marked's code blocks take);
    // any other class, such as layout utilities in a reply, is dropped.
    const CLASSES = new Set([
        'code-block-wrapper', 'code-header', 'code-language', 'copy-btn',
        'btn', 'btn-app-ghost', 'btn-sm', 'bi', 'bi-clipboard', 'bi-check-lg', 'mermaid',
    ]);
    const LANGUAGE_CLASS = /^language-[A-Za-z0-9_+#.-]{1,40}$/;

    const CONFIG = {
        USE_PROFILES: { html: true },
        FORBID_TAGS: [
            'style', 'link', 'meta', 'base', 'form', 'textarea', 'select', 'option',
            'iframe', 'frame', 'frameset', 'object', 'embed', 'template',
            'audio', 'video', 'source', 'track', 'picture',
        ],
        FORBID_ATTR: [
            'style', 'id', 'name', 'form', 'formaction', 'action', 'srcset', 'ping',
            'poster', 'background',
        ],
        ALLOW_DATA_ATTR: false,
        ADD_ATTR: [COPY_ATTR],
    };

    let purifier = null;
    // The nodes of image placeholders, as the sanitiser's own hook made them.
    const made = new WeakSet();

    // Where an image comes from: 'own' (SPARK's origin, or a data image), 'remote' (https,
    // which the reader may load) or 'text' (anything else: shown as text and never loaded).
    function classifyImage(src) {
        const raw = String(src == null ? '' : src).trim();
        if (!raw) return { kind: 'text', host: '' };
        if (OWN_DATA_IMAGE.test(raw)) return { kind: 'own' };
        const relative = PROTOCOL_RELATIVE.test(raw);
        let url;
        try {
            url = new URL(relative ? 'https:' + raw.replace(/\\/g, '/') : raw, document.baseURI);
        } catch (error) {
            return { kind: 'text', host: '' };
        }
        const host = url.host || (url.protocol === 'data:' ? 'embedded data' : url.protocol);
        if (relative) return { kind: 'text', host };
        if ((url.protocol === 'http:' || url.protocol === 'https:')
            && url.origin === window.location.origin) return { kind: 'own' };
        if (url.protocol === 'https:') return { kind: 'remote', host, href: url.href };
        return { kind: 'text', host };
    }

    // The stand-in for an image that is not shown: its alt text and its host, as a button
    // (a keyboard stop) when the reader may load it, otherwise as plain text.
    function imagePlaceholder(doc, alt, info) {
        const holder = doc.createElement('span');
        made.add(holder);
        if (info.kind === 'remote') {
            holder.setAttribute('class', 'md-remote-image');
            holder.setAttribute('role', 'button');
            holder.setAttribute('tabindex', '0');
            holder.setAttribute(IMAGE_ATTR, info.href);
            holder.setAttribute('title', `Load this image from ${info.host}`);
        } else {
            holder.setAttribute('class', 'md-remote-image md-remote-image-blocked');
            holder.setAttribute('title', 'This image is not loaded');
        }
        const icon = doc.createElement('i');
        made.add(icon);
        icon.setAttribute('class', 'bi bi-image');
        icon.setAttribute('aria-hidden', 'true');
        const label = doc.createElement('span');
        made.add(label);
        label.setAttribute('class', 'md-remote-image-alt');
        label.textContent = alt || 'image';
        const where = doc.createElement('span');
        made.add(where);
        where.setAttribute('class', 'md-remote-image-host');
        where.textContent = info.host ? ` (${info.host})` : '';
        holder.append(icon, label, where);
        return holder;
    }

    function sanitizer() {
        if (purifier) return purifier;
        // An instance of its own, so these hooks touch no other use of DOMPurify.
        purifier = window.DOMPurify(window);
        purifier.addHook('uponSanitizeAttribute', (node, data) => {
            // The sanitiser walks on into the placeholder its own hook has just made;
            // what that hook wrote is kept as written (a reply cannot reach this set).
            if (made.has(node)) {
                data.forceKeepAttr = true;
                return;
            }
            if (data.attrName === 'class') {
                const kept = String(data.attrValue || '').split(/\s+/)
                    .filter(name => CLASSES.has(name) || LANGUAGE_CLASS.test(name));
                if (kept.length) data.attrValue = kept.join(' ');
                else data.keepAttr = false;
            } else if (data.attrName === COPY_ATTR) {
                data.attrValue = 'code';
            }
        });
        purifier.addHook('afterSanitizeAttributes', node => {
            const tag = node.tagName;
            if (tag === 'A' && node.hasAttribute('href')) {
                const href = node.getAttribute('href') || '';
                if (href.startsWith('#')) {
                    node.removeAttribute('target');
                } else {
                    node.setAttribute('target', '_blank');
                    node.setAttribute('rel', 'noopener noreferrer');
                }
            } else if (tag === 'IMG') {
                const info = classifyImage(node.getAttribute('src'));
                if (info.kind !== 'own') {
                    node.replaceWith(imagePlaceholder(
                        node.ownerDocument, node.getAttribute('alt'), info));
                }
            } else if (tag === 'INPUT') {
                // Only a task list's tick box: always a disabled checkbox.
                node.setAttribute('type', 'checkbox');
                node.setAttribute('disabled', '');
                node.removeAttribute('value');
            }
        });
        return purifier;
    }

    function escape(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Code blocks: a header with the language and a copy button; mermaid as its source,
    // as text, for main.js renderMermaidDiagrams.
    function code(token) {
        const text = token && typeof token === 'object' ? token.text : String(token || '');
        const lang = token && typeof token === 'object' ? token.lang : '';
        const language = String(lang || '').trim().split(/\s+/)[0] || '';
        if (language === 'mermaid') {
            return `<div class="mermaid">${escape(text)}</div>\n`;
        }
        const langClass = LANGUAGE_CLASS.test(`language-${language}`)
            ? ` class="language-${language}"` : '';
        return `<div class="code-block-wrapper">
            <div class="code-header">
                <span class="code-language">${escape(language || 'text')}</span>
                <button type="button" class="btn btn-app-ghost btn-sm copy-btn" ${COPY_ATTR}="code" title="Copy" aria-label="Copy code">
                    <i class="bi bi-clipboard"></i>
                </button>
            </div>
            <pre><code${langClass}>${escape(text)}</code></pre>
        </div>\n`;
    }

    let parser = null;

    function markdown() {
        if (parser) return parser;
        parser = new window.marked.Marked({ gfm: true, breaks: true, async: false });
        parser.use({ renderer: { code } });
        return parser;
    }

    function sanitize(html) {
        return sanitizer().sanitize(String(html == null ? '' : html), CONFIG);
    }

    function render(text) {
        return sanitize(markdown().parse(String(text == null ? '' : text), { async: false }));
    }

    // A reply that is still streaming, drawn into its element block by block. Each
    // top-level block (paragraph, list, table, code block...) is parsed and sanitised on
    // its own, into nodes (no serialising and re-parsing); a block whose source has not
    // changed since the last render keeps its nodes, so a long reply costs only its
    // changing tail. Every new or changed block is sanitised on every render.
    const streams = new WeakMap();

    function renderInto(el, text) {
        const source = String(text == null ? '' : text);
        const md = markdown();
        const tokens = md.lexer(source);
        const links = JSON.stringify(tokens.links || {});
        let state = streams.get(el);
        const intact = state && state.links === links
            && el.childNodes.length === state.blocks.reduce((n, b) => n + b.nodes.length, 0)
            && state.blocks.every(b => b.nodes.every(node => node.parentNode === el));
        if (!intact) {
            el.textContent = '';
            state = { links, blocks: [] };
            streams.set(el, state);
        }
        let same = 0;
        while (same < state.blocks.length && same < tokens.length
            && state.blocks[same].raw === tokens[same].raw) same++;
        state.blocks.splice(same).forEach(b => b.nodes.forEach(node => node.remove()));
        for (let i = same; i < tokens.length; i++) {
            const block = [tokens[i]];
            block.links = tokens.links;
            const html = md.parser(block);
            const fragment = sanitizer().sanitize(html, { ...CONFIG, RETURN_DOM_FRAGMENT: true });
            const nodes = Array.from(fragment.childNodes);
            el.appendChild(fragment);
            state.blocks.push({ raw: tokens[i].raw, nodes });
        }
    }

    // The copy button of a rendered code block, answered for every page at once.
    function copyCode(button) {
        const wrapper = button.closest('.code-block-wrapper');
        const block = wrapper && wrapper.querySelector('pre code');
        if (!block || !navigator.clipboard) return;
        navigator.clipboard.writeText(block.textContent).then(() => {
            const icon = button.querySelector('i');
            if (!icon) return;
            icon.classList.replace('bi-clipboard', 'bi-check-lg');
            setTimeout(() => icon.classList.replace('bi-check-lg', 'bi-clipboard'), 2000);
        }).catch(() => {});
    }

    // A remote image the reader chose to load: only an https address is ever loaded, and
    // the placeholder is checked again here, whatever page it was found in.
    function loadImage(holder) {
        let url;
        try {
            url = new URL(holder.getAttribute(IMAGE_ATTR));
        } catch (error) {
            return;
        }
        if (url.protocol !== 'https:') return;
        const labelled = holder.querySelector('.md-remote-image-alt');
        const alt = labelled ? labelled.textContent : '';
        holder.removeAttribute(IMAGE_ATTR);
        holder.removeAttribute('role');
        holder.removeAttribute('tabindex');
        holder.removeAttribute('title');
        holder.classList.add('md-remote-image-loaded');
        const img = document.createElement('img');
        img.alt = alt;
        img.referrerPolicy = 'no-referrer';
        img.addEventListener('error', () => {
            holder.classList.remove('md-remote-image-loaded');
            holder.classList.add('md-remote-image-blocked');
            holder.textContent = `${alt || 'image'} (${url.host}) could not be loaded`;
        });
        img.src = url.href;
        holder.replaceChildren(img);
    }

    document.addEventListener('click', event => {
        const target = event.target;
        if (!target || !target.closest) return;
        const image = target.closest(`[${IMAGE_ATTR}]`);
        if (image) {
            // Also stops a link around the image from being followed.
            event.preventDefault();
            loadImage(image);
            return;
        }
        const button = target.closest(`[${COPY_ATTR}]`);
        if (!button) return;
        event.preventDefault();
        copyCode(button);
    });

    document.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const target = event.target;
        const image = target && target.closest ? target.closest(`[${IMAGE_ATTR}]`) : null;
        if (!image) return;
        event.preventDefault();
        loadImage(image);
    });

    window.SparkMarkdown = {
        render,
        renderInto,
        safe: render,
        sanitize,
        escape,
    };
})();
