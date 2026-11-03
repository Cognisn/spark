/**
 * Spark: attach files to a chat message.
 *
 * The attach button uploads each chosen file to the user's own file area
 * (POST /files/api, with the conversation id) through the page's fetch, which
 * csrf.js wraps with the session's CSRF token. Uploaded files wait as chips
 * above the composer; sending the message passes their ids to the send route,
 * which names them at the end of the message the model receives:
 *
 *   [Attached files: read them with the read_my_file tool]
 *   - file 12: report.pdf
 *
 * parse() reads that note back, so the message (live or from history) shows
 * its text with the files as chips linking to their downloads. A file over the
 * upload limit is refused before it is sent; refusals from the server (upload
 * limit, quota, storage) are shown above the composer, naming the file.
 */
(function (root) {
    'use strict';

    const MARKER = '[Attached files: read them with the read_my_file tool]';
    const LINE = /^- file (\d+): (.+)$/;

    function formatBytes(n) {
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = Number(n) || 0;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        if (unit === 0) return `${value} B`;
        return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
    }

    function escapeText(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /** The message text and its attached files, from content the send routes wrote. */
    function parse(content) {
        const text = String(content || '');
        let start = -1;
        if (text.startsWith(MARKER + '\n')) {
            start = 0;
        } else {
            start = text.lastIndexOf('\n\n' + MARKER + '\n');
        }
        if (start < 0) return { text: text, files: [] };
        const noteAt = start === 0 ? 0 : start + 2;
        const lines = text.slice(noteAt + MARKER.length + 1).split('\n');
        const files = [];
        for (const line of lines) {
            const match = LINE.exec(line);
            if (!match) return { text: text, files: [] };
            files.push({ id: Number(match[1]), name: match[2] });
        }
        return { text: text.slice(0, start), files: files };
    }

    function chipsHtml(files) {
        return files.map((f) =>
            `<a class="attachment-chip" href="/files/api/${encodeURIComponent(f.id)}" ` +
            `title="Download ${escapeText(f.name)}">` +
            `<i class="bi bi-paperclip"></i><span>${escapeText(f.name)}</span></a>`
        ).join('');
    }

    function refusal(name, status, body, limits) {
        const lead = `${name} was not attached:`;
        const max = limits && limits.max_upload_bytes;
        if (status === 413) {
            return `${lead} it is larger than the upload limit` +
                (max ? ` of ${formatBytes(max)}.` : '.');
        }
        if (status === 507) {
            const quota = limits && limits.quota_bytes;
            const used = limits && limits.usage_bytes;
            const detail = quota ? ` (${formatBytes(used || 0)} of ${formatBytes(quota)} in use)` : '';
            return `${lead} your file storage is full${detail}. ` +
                'Delete files you no longer need, or ask an administrator for more space.';
        }
        if (status === 503) return `${lead} file storage is not available on this server.`;
        if (status === 403) return `${lead} the upload was refused. Reload the page and try again.`;
        if (status === 401) return `${lead} sign in again to attach files.`;
        if (status === 404) return `${lead} this conversation was not found.`;
        const said = body && typeof body.error === 'string' ? ` ${body.error}` : '';
        return `${lead} the upload failed (${status}).${said}`;
    }

    /** The user's upload limit, quota and usage, from the files listing. */
    async function limits() {
        const resp = await fetch('/files/api', { headers: { Accept: 'application/json' } });
        if (!resp.ok) return null;
        const body = await resp.json();
        return {
            usage_bytes: body.usage_bytes,
            quota_bytes: body.quota_bytes,
            max_upload_bytes: body.max_upload_bytes,
        };
    }

    /** Upload one file to the user's area; resolves {ok, file} or {ok: false, message}. */
    async function upload(conversationId, file, known) {
        const max = known && known.max_upload_bytes;
        if (max && file.size > max) {
            return { ok: false, message: refusal(file.name, 413, null, known) };
        }
        const form = new FormData();
        form.append('file', file, file.name);
        if (conversationId) form.append('conversation_id', String(conversationId));
        let resp;
        try {
            resp = await fetch('/files/api', { method: 'POST', body: form });
        } catch (err) {
            const why = err && err.message ? err.message : 'network error';
            return { ok: false, message: `${file.name} was not attached: the upload failed (${why}).` };
        }
        let body = {};
        try {
            body = await resp.json();
        } catch (err) {
            body = {};
        }
        if (resp.status === 201 && body.file) {
            return {
                ok: true,
                file: { id: body.file.id, name: body.file.filename, size: body.file.size_bytes },
            };
        }
        return { ok: false, message: refusal(file.name, resp.status, body, known) };
    }

    // -- The composer -----------------------------------------------------------------

    const pending = [];
    let known = null;
    let uploading = 0;
    let composerConversation = null;

    function byId(id) {
        return root.document && root.document.getElementById
            ? root.document.getElementById(id) : null;
    }

    function renderPending() {
        const box = byId('attach-pending');
        if (!box) return;
        box.innerHTML = pending.map((p, i) =>
            `<span class="attachment-chip pending${p.file ? '' : ' uploading'}">` +
            `<i class="bi ${p.file ? 'bi-paperclip' : 'bi-arrow-repeat'}"></i>` +
            `<span>${escapeText(p.name)}</span>` +
            (p.file ? `<button type="button" class="attachment-remove" data-index="${i}" ` +
                `title="Remove ${escapeText(p.name)}" aria-label="Remove ${escapeText(p.name)}">` +
                '<i class="bi bi-x"></i></button>' : '') +
            '</span>'
        ).join('');
        box.style.display = pending.length ? 'flex' : 'none';
    }

    function showRefusal(message) {
        const box = byId('attach-errors');
        if (!box) return;
        const item = root.document.createElement('div');
        item.className = 'alert-app alert-app-danger attach-error';
        item.setAttribute('role', 'alert');
        item.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>${escapeText(message)}` +
            '<button type="button" class="attachment-remove" aria-label="Dismiss"><i class="bi bi-x"></i></button>';
        item.querySelector('button').addEventListener('click', () => item.remove());
        box.appendChild(item);
    }

    function clearRefusals() {
        const box = byId('attach-errors');
        if (box) box.innerHTML = '';
    }

    async function attachFiles(fileList) {
        clearRefusals();
        if (!known) {
            try {
                known = await limits();
            } catch (err) {
                known = null;
            }
        }
        await Promise.all(Array.from(fileList).map(async (file) => {
            const entry = { name: file.name, file: null };
            pending.push(entry);
            uploading += 1;
            renderPending();
            const result = await upload(composerConversation, file, known);
            uploading -= 1;
            if (result.ok) {
                entry.file = result.file;
                if (known && known.usage_bytes != null) known.usage_bytes += result.file.size || 0;
            } else {
                pending.splice(pending.indexOf(entry), 1);
                showRefusal(result.message);
            }
            renderPending();
        }));
    }

    /** Wire the attach button, file input and pending chips for one conversation. */
    function initComposer(conversationId) {
        composerConversation = conversationId;
        const button = byId('btn-attach');
        const input = byId('attach-input');
        const box = byId('attach-pending');
        if (!button || !input) return;
        button.addEventListener('click', () => input.click());
        input.addEventListener('change', () => {
            const chosen = input.files;
            if (chosen && chosen.length) attachFiles(chosen).finally(() => { input.value = ''; });
        });
        if (box) {
            box.addEventListener('click', (event) => {
                const target = event.target.closest('.attachment-remove');
                if (!target) return;
                pending.splice(Number(target.dataset.index), 1);
                renderPending();
            });
        }
        renderPending();
    }

    /** Whether an upload is still in progress (the message waits for it). */
    function busy() {
        return uploading > 0;
    }

    /** The uploaded files waiting to be sent, removed from the composer. */
    function take() {
        const ready = pending.filter((p) => p.file).map((p) => ({ id: p.file.id, name: p.file.name }));
        pending.length = 0;
        renderPending();
        clearRefusals();
        return ready;
    }

    /** Add a message's attachment chips to its bubble. */
    function decorate(bubble, files) {
        if (!bubble || !files || !files.length) return;
        const row = root.document.createElement('div');
        row.className = 'attachment-chips';
        row.innerHTML = chipsHtml(files);
        const body = bubble.querySelector('.message-body');
        if (body && body.parentNode) {
            body.parentNode.insertBefore(row, body.nextSibling);
        } else {
            bubble.appendChild(row);
        }
    }

    root.SparkAttachments = {
        MARKER: MARKER,
        busy: busy,
        chipsHtml: chipsHtml,
        decorate: decorate,
        formatBytes: formatBytes,
        initComposer: initComposer,
        limits: limits,
        parse: parse,
        refusal: refusal,
        take: take,
        upload: upload,
    };
})(typeof window !== 'undefined' ? window : globalThis);
