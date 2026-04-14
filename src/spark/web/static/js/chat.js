/**
 * Spark — Chat message rendering and interaction logic
 * Handles message display, tool call visualization, and UI controls.
 */

const TOOL_RESULTS_MARKER = '[TOOL_RESULTS]';

// Running token totals
let totalTokensIn = 0;
let totalTokensOut = 0;
let totalCacheRead = 0;
let totalCacheCreate = 0;

function updateTokenDisplay(inputTokens, outputTokens, cacheRead, cacheCreate) {
    totalTokensIn += inputTokens || 0;
    totalTokensOut += outputTokens || 0;
    const inEl = document.getElementById('tc-in');
    const outEl = document.getElementById('tc-out');
    if (inEl) inEl.textContent = totalTokensIn.toLocaleString();
    if (outEl) outEl.textContent = totalTokensOut.toLocaleString();

    // Update cache stats if present
    if (cacheRead || cacheCreate) {
        totalCacheRead += cacheRead || 0;
        totalCacheCreate += cacheCreate || 0;
        const cacheEl = document.getElementById('tc-cache');
        const readEl = document.getElementById('tc-cache-read');
        const createEl = document.getElementById('tc-cache-create');
        const savingsEl = document.getElementById('tc-cache-savings');
        if (cacheEl) cacheEl.style.display = '';
        if (readEl) readEl.textContent = totalCacheRead.toLocaleString();
        if (createEl) createEl.textContent = totalCacheCreate.toLocaleString();

        // Calculate cost savings percentage
        // Without cache: all tokens at full price
        // With cache: uncached at full, cache_read at 10%, cache_create at 125%
        if (savingsEl && (totalCacheRead > 0 || totalCacheCreate > 0)) {
            const withoutCache = totalTokensIn + totalCacheRead + totalCacheCreate;
            const withCache = totalTokensIn + (totalCacheRead * 0.1) + (totalCacheCreate * 1.25);
            const savingsPct = withoutCache > 0 ? ((1 - withCache / withoutCache) * 100) : 0;
            if (savingsPct > 0) {
                savingsEl.textContent = `(${Math.round(savingsPct)}% saved)`;
                savingsEl.style.display = '';
            }
        }
    }
}

function localTime() {
    return new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function clearEmptyState() {
    const container = document.getElementById('chat-messages');
    const empty = container.querySelector('.chat-empty-state');
    if (empty) empty.remove();
}


/* ==========================================================================
   1. Chat History
   ========================================================================== */

let _loadingHistory = false;

async function loadChatHistory(conversationId) {
    _loadingHistory = true;
    const container = document.getElementById('chat-messages');
    try {
        const resp = await fetch(`/chat/${conversationId}/api/history`);
        const messages = await resp.json();
        container.innerHTML = '';

        if (!messages.length) {
            container.innerHTML = `
                <div class="chat-empty-state text-center py-5" style="color: var(--app-text-muted);">
                    <i class="bi bi-chat-dots" style="font-size: 2.5rem;"></i>
                    <p class="mt-2">Start the conversation by sending a message.</p>
                </div>`;
            return;
        }

        messages.forEach(msg => appendMessage(msg));
        _loadingHistory = false;
        scrollToBottom();

        // Load tool activity into the sidecar panel from the database
        await loadToolActivity(conversationId);
        // Load agent run history into the agents tab
        await loadAgentHistory(conversationId);
    } catch (err) {
        container.innerHTML = `<div class="alert-app alert-app-danger">Failed to load history.</div>`;
    }
}

async function loadToolActivity(conversationId) {
    try {
        const resp = await fetch(`/chat/${conversationId}/api/tool-activity`);
        const transactions = await resp.json();
        if (!transactions.length) return;

        // Clear the default "No tool calls yet" placeholder
        clearToolPanel();

        // Group transactions by date
        const groups = {};
        transactions.forEach(t => {
            const dateKey = _formatToolDate(t.timestamp);
            if (!groups[dateKey]) groups[dateKey] = [];
            groups[dateKey].push(t);
        });

        // Render each date group
        Object.entries(groups).forEach(([dateKey, items]) => {
            // Pre-create the group — _addToolPanelItem will increment counts
            _ensureDateGroup(dateKey, 0);
            items.forEach(t => {
                let params = {};
                try { params = JSON.parse(t.tool_input || '{}'); } catch (e) {}
                const status = t.is_error ? 'error' : 'success';
                const result = t.tool_response || null;
                _addToolPanelItem(t.tool_name, params, status, result, t.timestamp, dateKey);
            });
        });
    } catch (err) {
        // Non-critical — sidecar just won't show history
    }
}

function appendMessage(msg) {
    const content = msg.content || '';
    const role = msg.role || 'user';
    const timestamp = msg.timestamp || null;

    // Tool results marker
    if (content.startsWith(TOOL_RESULTS_MARKER)) {
        try {
            const results = JSON.parse(content.substring(TOOL_RESULTS_MARKER.length));
            appendToolResults(results);
        } catch (e) {
            appendTextMessage(role, content, timestamp);
        }
        return;
    }

    // JSON content blocks (assistant with tool_use)
    if (content.startsWith('[')) {
        try {
            const blocks = JSON.parse(content);
            if (Array.isArray(blocks) && blocks.length > 0) {
                // Skip tool_result-only blocks (already shown in tool group)
                const onlyToolResults = blocks.every(b => b.type === 'tool_result');
                if (onlyToolResults) return;

                // Check there's at least one meaningful block
                const hasContent = blocks.some(b =>
                    (b.type === 'text' && b.text) || (b.type === 'tool_use' && b.name)
                );
                if (hasContent) {
                    appendContentBlocks(role, blocks, timestamp);
                    return;
                }
            }
        } catch (e) {
            // Fall through to text
        }
    }

    // Compacted context
    if (content.startsWith('[COMPACTED CONTEXT') || content.startsWith('[EMERGENCY TRUNCATION')) {
        appendSystemMessage(content.split('\n')[0]);
        return;
    }

    appendTextMessage(role, content, timestamp, msg.model_id || null);
}


/* ==========================================================================
   2. Message Rendering
   ========================================================================== */

function _formatDateTime(timestamp) {
    const d = timestamp ? new Date(timestamp) : new Date();
    return d.toLocaleString(undefined, {
        day: 'numeric', month: 'short',
        hour: '2-digit', minute: '2-digit',
    });
}

function _buildMessageHeader(role) {
    const icon = role === 'assistant'
        ? '<i class="bi bi-robot" style="color: var(--app-accent);"></i>'
        : '<i class="bi bi-person" style="color: var(--app-text-secondary);"></i>';
    const label = role === 'assistant' ? 'Spark' : 'You';

    return `
        <div class="message-header">
            <span class="d-flex align-items-center gap-1">
                ${icon}
                <strong style="font-size: 0.8125rem;">${label}</strong>
            </span>
        </div>
    `;
}

function _buildTimestamp(timestamp, modelId) {
    const modelBadge = modelId
        ? `<span class="message-model">${modelId}</span>`
        : '';
    return `<div class="message-timestamp">${_formatDateTime(timestamp)}${modelBadge}</div>`;
}

function _buildCopyButton(contentText) {
    return `
        <div class="message-actions">
            <button class="btn btn-app-ghost" style="padding: 0.125rem 0.375rem; font-size: 0.75rem;"
                    onclick="copyBubble(this)" title="Copy to clipboard"
                    data-copy-text="${encodeURIComponent(contentText)}">
                <i class="bi bi-clipboard"></i>
            </button>
        </div>
    `;
}

function appendTextMessage(role, content, timestamp, modelId) {
    clearEmptyState();
    const container = document.getElementById('chat-messages');

    const wrapper = document.createElement('div');
    wrapper.className = `chat-message-wrapper ${role}`;

    const bubble = document.createElement('div');
    bubble.className = `chat-message ${role}`;

    const header = _buildMessageHeader(role);
    const copyBtn = _buildCopyButton(content);

    if (role === 'assistant') {
        bubble.innerHTML = `${header}<div class="markdown-content">${renderMarkdown(content)}</div>${copyBtn}`;
        highlightCodeBlocks(bubble);
        renderMermaidDiagrams(bubble);
    } else {
        bubble.innerHTML = `${header}<div class="message-body">${escapeHtml(content)}</div>${copyBtn}`;
    }

    // Show model badge on assistant messages
    const badge = (role === 'assistant') ? (modelId || window._currentModelId || '') : null;
    wrapper.appendChild(bubble);
    wrapper.insertAdjacentHTML('beforeend', _buildTimestamp(timestamp, badge));
    container.appendChild(wrapper);
}

function appendSystemMessage(text) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'chat-message system';
    div.innerHTML = `<i class="bi bi-info-circle me-1"></i>${escapeHtml(text)}`;
    container.appendChild(div);
}

function appendContentBlocks(role, blocks, timestamp) {
    const textParts = [];
    const toolCalls = [];

    blocks.forEach(block => {
        if (!block || typeof block !== 'object') return;
        if (block.type === 'text' && block.text) {
            textParts.push(block.text);
        } else if (block.type === 'tool_use' && block.name) {
            toolCalls.push(block);
        }
    });

    if (textParts.length) {
        appendTextMessage(role, textParts.join(''), timestamp);
    }

    if (toolCalls.length) {
        // Add to sidecar panel + inline indicator in chat
        addToolCallsToPanel(toolCalls, timestamp);
        appendToolInlineIndicator(toolCalls.length);
    }
}


/* ==========================================================================
   3. Tool Call Display — Sidecar Panel
   ========================================================================== */

let _toolPanelCount = 0;
let _dateGroupCounts = {};

function _formatToolTime(timestamp) {
    const d = timestamp ? new Date(timestamp) : new Date();
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function _formatToolDate(timestamp) {
    const d = timestamp ? new Date(timestamp) : new Date();
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

function _ensureDateGroup(dateKey, initialCount) {
    const groupId = 'tp-date-' + dateKey.replace(/\s+/g, '-').replace(/[^a-zA-Z0-9-]/g, '');
    if (document.getElementById(groupId)) return groupId;

    const panel = document.getElementById('tool-panel-body');
    const count = initialCount || 0;
    _dateGroupCounts[groupId] = count;

    const group = document.createElement('div');
    group.className = 'tp-date-group';
    group.id = groupId;
    group.innerHTML = `
        <div class="tp-date-header" onclick="toggleDateGroup('${groupId}')">
            <i class="bi bi-chevron-down tp-date-chevron" id="${groupId}-chevron"></i>
            ${escapeHtml(dateKey)}
            <span class="tp-date-count" id="${groupId}-count">(${count} call${count !== 1 ? 's' : ''})</span>
        </div>
        <div class="tp-date-body" id="${groupId}-body"></div>
    `;
    panel.appendChild(group);
    return groupId;
}

function _updateDateGroupCount(groupId) {
    const countEl = document.getElementById(groupId + '-count');
    const count = _dateGroupCounts[groupId] || 0;
    if (countEl) countEl.textContent = `(${count} call${count !== 1 ? 's' : ''})`;
}

function toggleDateGroup(groupId) {
    const body = document.getElementById(groupId + '-body');
    const chevron = document.getElementById(groupId + '-chevron');
    if (!body) return;
    const isCollapsed = body.classList.contains('collapsed');
    body.classList.toggle('collapsed');
    if (chevron) chevron.classList.toggle('collapsed', !isCollapsed);
}

function _addToolPanelItem(toolName, params, status, result, timestamp, dateKey) {
    const panel = document.getElementById('tool-panel-body');
    const empty = document.getElementById('tool-panel-empty');
    if (empty) empty.style.display = 'none';

    _toolPanelCount++;
    _updateToolPanelBadge();

    // Determine the date group and target container
    if (!dateKey) dateKey = _formatToolDate(timestamp);
    const groupId = _ensureDateGroup(dateKey, 0);
    // Increment the count for streaming calls (history sets initial count upfront)
    if (!_dateGroupCounts[groupId]) _dateGroupCounts[groupId] = 0;
    _dateGroupCounts[groupId]++;
    _updateDateGroupCount(groupId);
    const targetContainer = document.getElementById(groupId + '-body') || panel;

    const itemId = 'tp-' + Math.random().toString(36).substr(2, 8);
    const toolIcon = getToolIcon(toolName || '');
    const statusIcons = {
        running: '<i class="bi bi-arrow-repeat tp-status running"></i>',
        success: '<i class="bi bi-check-circle-fill tp-status success"></i>',
        error: '<i class="bi bi-x-circle-fill tp-status error"></i>',
    };
    const statusIcon = statusIcons[status] || '';
    const timeStr = _formatToolTime(timestamp);

    const paramsJson = JSON.stringify(params || {}, null, 2);
    const resultText = result ? (typeof result === 'string' ? result.trim() : JSON.stringify(result, null, 2)) : null;

    let detailHtml = '';
    if (paramsJson !== '{}' && paramsJson.length > 2) {
        detailHtml += `<div class="tp-detail-section">Parameters</div><div class="tp-detail-code">${escapeHtml(paramsJson)}</div>`;
    }
    if (resultText) {
        detailHtml += `<div class="tp-detail-section">Result</div><div class="tp-detail-code">${escapeHtml(resultText)}</div>`;
    }

    const item = document.createElement('div');
    item.className = 'tool-panel-item';
    item.id = itemId;
    item.innerHTML = `
        <div class="tp-header" onclick="toggleToolPanelItem('${itemId}')">
            <i class="bi ${toolIcon} tp-icon"></i>
            <span class="tp-name">${escapeHtml(toolName || 'Unknown')}</span>
            <span class="tp-time">${timeStr}</span>
            ${statusIcon}
        </div>
        <div class="tp-detail" id="${itemId}-detail">${detailHtml}</div>
    `;

    targetContainer.appendChild(item);
    panel.scrollTop = panel.scrollHeight;
    return itemId;
}

function updateToolPanelItem(itemId, status, result) {
    const item = document.getElementById(itemId);
    if (!item) return;

    // Update status icon
    const header = item.querySelector('.tp-header');
    const oldStatus = header.querySelector('.tp-status');
    if (oldStatus) oldStatus.remove();
    const statusIcons = {
        success: '<i class="bi bi-check-circle-fill tp-status success"></i>',
        error: '<i class="bi bi-x-circle-fill tp-status error"></i>',
    };
    header.insertAdjacentHTML('beforeend', statusIcons[status] || '');

    // Add result to detail
    if (result) {
        const detail = item.querySelector('.tp-detail');
        const resultText = typeof result === 'string' ? result.trim() : JSON.stringify(result, null, 2);
        detail.innerHTML += `<div class="tp-detail-section">Result</div><div class="tp-detail-code">${escapeHtml(resultText)}</div>`;
    }
}

function _updateToolPanelBadge() {
    const badge = document.getElementById('tool-panel-badge');
    const total = document.getElementById('tool-panel-total');
    if (badge) {
        if (_toolPanelCount > 0) {
            badge.textContent = _toolPanelCount;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    }
    if (total) total.textContent = _toolPanelCount;
}

function toggleToolPanel() {
    const panel = document.getElementById('tool-panel');
    const isOpen = panel.style.display === 'flex';
    panel.style.display = isOpen ? 'none' : 'flex';
    sessionStorage.setItem('spark-tool-panel', isOpen ? 'closed' : 'open');
}

function clearToolPanel() {
    const panel = document.getElementById('tool-panel-body');
    panel.innerHTML = `
        <div id="tool-panel-empty" class="text-center py-4" style="color: var(--app-text-muted);">
            <i class="bi bi-tools" style="font-size: 1.5rem; opacity: 0.3;"></i>
            <p class="mt-2 mb-0" style="font-size: 0.75rem;">No tool calls yet</p>
        </div>`;
    _toolPanelCount = 0;
    _dateGroupCounts = {};
    _updateToolPanelBadge();
}

function toggleToolPanelItem(itemId) {
    const detail = document.getElementById(itemId + '-detail');
    if (!detail) return;
    const isHidden = getComputedStyle(detail).display === 'none';
    detail.style.display = isHidden ? 'block' : 'none';
}

// Add completed tool calls from history to the sidecar panel
function addToolCallsToPanel(toolCalls, timestamp) {
    // During history load, skip — loadToolActivity() populates the panel
    // with complete data (params + results) from the database.
    if (_loadingHistory) return;
    toolCalls.forEach(tc => {
        _addToolPanelItem(tc.name, tc.input, 'success', null, timestamp);
    });
}

// Inline indicator in chat area (replaces the old tool-call-group)
function appendToolInlineIndicator(count, statuses) {
    const container = document.getElementById('chat-messages');
    const dots = (statuses || []).map(s => `<span class="dot ${s}"></span>`).join('')
        || Array(Math.min(count, 6)).fill('<span class="dot success"></span>').join('');

    const div = document.createElement('div');
    div.className = 'tool-inline-indicator';
    div.onclick = () => { if (document.getElementById('tool-panel').style.display !== 'flex') toggleToolPanel(); };
    div.innerHTML = `
        <i class="bi bi-tools" style="font-size: 0.75rem;"></i>
        <span>Used ${count} tool${count !== 1 ? 's' : ''}</span>
        <div class="indicator-dots">${dots}</div>
    `;
    container.appendChild(div);
}

function appendToolResults(results) {
    // Tool results shown in sidecar panel, not inline.
}

function appendApprovalCard(toolName, params, toolUseId) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'approval-card';
    div.id = `approval-${toolUseId}`;
    div.innerHTML = `
        <div class="approval-header">
            <i class="bi bi-shield-exclamation"></i>
            <span>Action Required</span>
            <span class="badge-app badge-app-warning ms-auto" id="approval-badge-${toolUseId}">Pending</span>
        </div>
        <div class="approval-detail">
            <strong>${escapeHtml(toolName)}</strong> requires approval to execute.
            <pre style="margin-top: 0.5rem; font-size: 0.75rem;">${escapeHtml(JSON.stringify(params, null, 2))}</pre>
        </div>
        <div class="approval-actions d-flex flex-wrap gap-1">
            <button class="btn btn-app-ghost btn-sm" onclick="approveAction('${toolUseId}', 'denied')">
                <i class="bi bi-x-circle me-1"></i>Deny
            </button>
            <button class="btn btn-app-outline btn-sm" onclick="approveAction('${toolUseId}', 'once')">
                <i class="bi bi-check me-1"></i>Once
            </button>
            <button class="btn btn-app-primary btn-sm" onclick="approveAction('${toolUseId}', 'allowed')">
                <i class="bi bi-check-lg me-1"></i>Always (Conv)
            </button>
            <button class="btn btn-app-primary btn-sm" onclick="approveAction('${toolUseId}', 'allowed_global')">
                <i class="bi bi-check-all me-1"></i>Always (Global)
            </button>
        </div>
    `;
    container.appendChild(div);
}


/* ==========================================================================
   4. Streaming Message Updates
   ========================================================================== */

let streamingMessageDiv = null;
let streamingWrapperDiv = null;

function removeStreamingMessage() {
    if (streamingWrapperDiv) {
        streamingWrapperDiv.remove();
        streamingMessageDiv = null;
        streamingWrapperDiv = null;
    }
}

function startStreamingMessage() {
    clearEmptyState();
    const container = document.getElementById('chat-messages');

    streamingWrapperDiv = document.createElement('div');
    streamingWrapperDiv.className = 'chat-message-wrapper assistant';

    streamingMessageDiv = document.createElement('div');
    streamingMessageDiv.className = 'chat-message assistant';
    streamingMessageDiv.innerHTML = `
        ${_buildMessageHeader('assistant')}
        <div class="markdown-content"></div>
        <div class="streaming-indicator">
            <div class="dot-pulse"><span></span><span></span><span></span></div>
        </div>
    `;

    streamingWrapperDiv.appendChild(streamingMessageDiv);
    container.appendChild(streamingWrapperDiv);
    scrollToBottom();
}

function updateStreamingMessage(content) {
    if (!streamingMessageDiv) startStreamingMessage();
    const md = streamingMessageDiv.querySelector('.markdown-content');
    md.innerHTML = renderMarkdown(content);
    highlightCodeBlocks(md);
    scrollToBottom();
}

function finaliseStreamingMessage(content) {
    if (!streamingMessageDiv) startStreamingMessage();
    const md = streamingMessageDiv.querySelector('.markdown-content');
    md.innerHTML = renderMarkdown(content);
    highlightCodeBlocks(md);
    renderMermaidDiagrams(md);

    // Remove streaming indicator
    const indicator = streamingMessageDiv.querySelector('.streaming-indicator');
    if (indicator) indicator.remove();

    // Add copy button
    const actions = document.createElement('div');
    actions.innerHTML = _buildCopyButton(content);
    streamingMessageDiv.appendChild(actions.firstElementChild);

    // Add timestamp with model badge outside the bubble
    if (streamingWrapperDiv) {
        streamingWrapperDiv.insertAdjacentHTML('beforeend', _buildTimestamp(null, window._currentModelId || ''));
    }

    streamingMessageDiv = null;
    streamingWrapperDiv = null;
    scrollToBottom();
}

// Streaming tool tracking — routes to sidecar panel
let _streamingToolIds = {};  // toolUseId -> panelItemId
let _streamingToolCount = 0;
let _streamingToolStatuses = [];

function appendStreamingToolStart(toolName, params, toolUseId) {
    _streamingToolCount++;
    _streamingToolStatuses.push('running');

    // Auto-open panel if closed
    const panel = document.getElementById('tool-panel');
    if (panel.style.display !== 'flex') toggleToolPanel();

    // Add to sidecar panel
    const panelItemId = _addToolPanelItem(toolName, params, 'running', null, null);
    _streamingToolIds[toolUseId] = panelItemId;
}

function updateStreamingToolComplete(toolUseId, toolName, result, status) {
    const panelItemId = _streamingToolIds[toolUseId];
    if (panelItemId) {
        updateToolPanelItem(panelItemId, status, result);
    }
    // Update status tracking
    const idx = _streamingToolStatuses.indexOf('running');
    if (idx !== -1) _streamingToolStatuses[idx] = status;
}

function finaliseStreamingToolGroup() {
    if (_streamingToolCount > 0) {
        // Add inline indicator in chat
        appendToolInlineIndicator(_streamingToolCount, _streamingToolStatuses);
    }
    _streamingToolIds = {};
    _streamingToolCount = 0;
    _streamingToolStatuses = [];
}


/* ==========================================================================
   5. UI Helpers
   ========================================================================== */

function toggleToolGroup(groupId) {
    const body = document.getElementById(`${groupId}-body`);
    const chevron = document.getElementById(`${groupId}-chevron`);
    if (body.style.display === 'none') {
        body.style.display = 'flex';
        chevron.style.transform = 'rotate(180deg)';
    } else {
        body.style.display = 'none';
        chevron.style.transform = '';
    }
}

function toggleActionDetail(cardId) {
    const detail = document.getElementById(`${cardId}-detail`);
    const chevron = document.getElementById(`${cardId}-chevron`);
    if (detail) {
        if (detail.style.display === 'none') {
            detail.style.display = 'block';
            if (chevron) chevron.style.transform = 'rotate(180deg)';
        } else {
            detail.style.display = 'none';
            if (chevron) chevron.style.transform = '';
        }
    }
}

function getToolIcon(toolName) {
    const lower = toolName.toLowerCase();
    if (lower.includes('file') || lower.includes('read') || lower.includes('write')) return 'bi-file-earmark-text';
    if (lower.includes('search') || lower.includes('web')) return 'bi-search';
    if (lower.includes('database') || lower.includes('query')) return 'bi-database';
    if (lower.includes('time') || lower.includes('date')) return 'bi-clock';
    if (lower.includes('code') || lower.includes('execute')) return 'bi-code-slash';
    if (lower.includes('image') || lower.includes('vision')) return 'bi-image';
    return 'bi-gear';
}

function copyBubble(btn) {
    const text = decodeURIComponent(btn.dataset.copyText);
    navigator.clipboard.writeText(text).then(() => {
        const icon = btn.querySelector('i');
        icon.classList.replace('bi-clipboard', 'bi-check-lg');
        setTimeout(() => icon.classList.replace('bi-check-lg', 'bi-clipboard'), 2000);
    });
}

function scrollToBottom() {
    const container = document.getElementById('chat-messages');
    requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
    });
}

function handleKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        document.getElementById('chat-form').dispatchEvent(new Event('submit'));
    }
}

function handleSubmit(event) {
    event.preventDefault();
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message) return;

    clearEmptyState();
    appendTextMessage('user', message, null);
    input.value = '';
    scrollToBottom();

    sendMessageWithSSE(conversationId, message);
}

// showInfoModal() and showSettingsModal() are defined in the chat template

// Permission handling
async function respondPermission(decision) {
    if (pendingPermissionRequestId) {
        try {
            await fetch('/chat/permission/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    request_id: pendingPermissionRequestId,
                    decision: decision,
                }),
            });
        } catch (err) {
            console.error('Failed to send permission response:', err);
        }
        pendingPermissionRequestId = null;
    }
    bootstrap.Modal.getInstance(document.getElementById('permissionModal'))?.hide();
}

async function approveAction(toolUseId, decision) {
    try {
        await fetch('/chat/permission/respond', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ request_id: toolUseId, decision }),
        });
        const badge = document.getElementById(`approval-badge-${toolUseId}`);
        if (badge) {
            const isApproved = decision !== 'denied';
            badge.className = isApproved ? 'badge-app badge-app-success' : 'badge-app badge-app-danger';
            const labels = {
                once: 'Approved (Once)',
                allowed: 'Approved (Conversation)',
                allowed_global: 'Approved (Global)',
                denied: 'Denied',
            };
            badge.textContent = labels[decision] || (isApproved ? 'Approved' : 'Denied');
        }
        // Disable all buttons in this card to prevent double-clicks
        const card = document.getElementById(`approval-${toolUseId}`);
        if (card) card.querySelectorAll('.approval-actions button').forEach(b => b.disabled = true);
    } catch (err) {
        AppToast.danger('Error', 'Failed to send response.');
    }
}


/* ==========================================================================
   7. Agent Panel — sidecar tab for agent runs
   ========================================================================== */

let _agentPanelCount = 0;

function switchSidecarTab(tab) {
    const toolsTab = document.getElementById('sidecar-tab-tools');
    const agentsTab = document.getElementById('sidecar-tab-agents');
    const toolsBody = document.getElementById('tool-panel-body');
    const agentsBody = document.getElementById('agent-panel-body');

    if (tab === 'tools') {
        toolsTab.style.color = 'var(--app-accent)';
        toolsTab.style.borderBottom = '2px solid var(--app-accent)';
        agentsTab.style.color = 'var(--app-text-muted)';
        agentsTab.style.borderBottom = '2px solid transparent';
        toolsBody.style.display = '';
        agentsBody.style.display = 'none';
    } else {
        agentsTab.style.color = 'var(--app-accent)';
        agentsTab.style.borderBottom = '2px solid var(--app-accent)';
        toolsTab.style.color = 'var(--app-text-muted)';
        toolsTab.style.borderBottom = '2px solid transparent';
        agentsBody.style.display = '';
        toolsBody.style.display = 'none';
    }
}

function _updateAgentPanelBadge() {
    const badge = document.getElementById('agent-panel-badge');
    if (badge) {
        if (_agentPanelCount > 0) {
            badge.textContent = _agentPanelCount;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    }
}

function _addAgentPanelItem(agentName, agentId, task, status, resultText, toolCallsJson, timestamp, inputTokens, outputTokens) {
    const panel = document.getElementById('agent-panel-body');
    const empty = document.getElementById('agent-panel-empty');
    if (empty) empty.style.display = 'none';

    _agentPanelCount++;
    _updateAgentPanelBadge();

    const itemId = 'ap-' + agentId.replace(/[^a-zA-Z0-9]/g, '-');
    const statusIcons = {
        running: '<i class="bi bi-arrow-repeat ap-status running"></i>',
        completed: '<i class="bi bi-check-circle-fill ap-status success"></i>',
        error: '<i class="bi bi-x-circle-fill ap-status error"></i>',
    };
    const statusIcon = statusIcons[status] || statusIcons['running'];
    const taskTrunc = (task || '').length > 80 ? task.substring(0, 80) + '…' : (task || '');
    const timeStr = timestamp ? _formatToolTime(timestamp) : _formatToolTime(null);

    // Build tool calls detail if available
    let toolCallsHtml = '';
    if (toolCallsJson) {
        try {
            const calls = typeof toolCallsJson === 'string' ? JSON.parse(toolCallsJson) : toolCallsJson;
            if (Array.isArray(calls) && calls.length) {
                calls.forEach(tc => {
                    const tcStatus = tc.status === 'error' ? 'error' : 'success';
                    const tcIcon = tcStatus === 'error'
                        ? '<i class="bi bi-x-circle-fill" style="color: var(--app-danger); font-size: 0.625rem;"></i>'
                        : '<i class="bi bi-check-circle-fill" style="color: var(--app-success); font-size: 0.625rem;"></i>';
                    toolCallsHtml += `<div class="ap-tool-entry d-flex align-items-center gap-1">
                        ${tcIcon}
                        <span>${escapeHtml(tc.tool_name || tc.name || 'tool')}</span>
                    </div>`;
                });
            }
        } catch (e) { /* ignore parse errors */ }
    }

    let resultHtml = '';
    if (resultText) {
        const truncResult = resultText.length > 200 ? resultText.substring(0, 200) + '…' : resultText;
        resultHtml = `<div class="tp-detail-section" style="margin-top: 0.375rem;">Result</div>
            <div class="tp-detail-code">${escapeHtml(truncResult)}</div>`;
    }

    let tokenHtml = '';
    if (inputTokens || outputTokens) {
        tokenHtml = `<div style="font-size: 0.625rem; color: var(--app-text-muted); margin-top: 0.25rem;">
            Tokens: ${(inputTokens || 0).toLocaleString()} in / ${(outputTokens || 0).toLocaleString()} out
        </div>`;
    }

    const item = document.createElement('div');
    item.className = 'agent-panel-item';
    item.id = itemId;
    item.innerHTML = `
        <div class="ap-header" onclick="toggleAgentPanelItem('${itemId}')">
            <i class="bi bi-robot" style="color: var(--app-accent); font-size: 0.875rem;"></i>
            <span class="ap-name">${escapeHtml(agentName || 'Agent')}</span>
            <span class="tp-time">${timeStr}</span>
            ${statusIcon}
        </div>
        <div class="ap-task">${escapeHtml(taskTrunc)}</div>
        <div class="ap-detail" id="${itemId}-detail">
            <div id="${itemId}-tools">${toolCallsHtml}</div>
            ${resultHtml}
            ${tokenHtml}
        </div>
    `;

    panel.appendChild(item);
    panel.scrollTop = panel.scrollHeight;
    return itemId;
}

function toggleAgentPanelItem(itemId) {
    const detail = document.getElementById(itemId + '-detail');
    if (!detail) return;
    const isHidden = getComputedStyle(detail).display === 'none';
    detail.style.display = isHidden ? 'block' : 'none';
}

function appendStreamingAgentStart(agentName, agentId, task, modelId) {
    // Switch to agents tab and auto-open panel
    switchSidecarTab('agents');
    const panel = document.getElementById('tool-panel');
    if (panel.style.display !== 'flex') toggleToolPanel();

    _addAgentPanelItem(agentName, agentId, task, 'running', null, null, null, 0, 0);
}

function updateStreamingAgentToolCall(agentId, toolName, params) {
    const itemId = 'ap-' + agentId.replace(/[^a-zA-Z0-9]/g, '-');
    const toolsContainer = document.getElementById(itemId + '-tools');
    if (!toolsContainer) return;

    const entryId = itemId + '-tool-' + Math.random().toString(36).substr(2, 6);
    const entry = document.createElement('div');
    entry.className = 'ap-tool-entry d-flex align-items-center gap-1';
    entry.id = entryId;
    entry.dataset.toolName = toolName;
    entry.innerHTML = `
        <i class="bi bi-arrow-repeat" style="color: var(--app-accent); font-size: 0.625rem; animation: app-spin 0.75s linear infinite;"></i>
        <span>${escapeHtml(toolName || 'tool')}</span>
    `;
    toolsContainer.appendChild(entry);

    // Auto-expand the detail section
    const detail = document.getElementById(itemId + '-detail');
    if (detail) detail.style.display = 'block';
}

function updateStreamingAgentToolResult(agentId, toolName, result, status) {
    const itemId = 'ap-' + agentId.replace(/[^a-zA-Z0-9]/g, '-');
    const toolsContainer = document.getElementById(itemId + '-tools');
    if (!toolsContainer) return;

    // Find the last matching running tool entry
    const entries = toolsContainer.querySelectorAll('.ap-tool-entry');
    for (let i = entries.length - 1; i >= 0; i--) {
        const entry = entries[i];
        if (entry.dataset.toolName === toolName && entry.querySelector('.bi-arrow-repeat')) {
            const icon = entry.querySelector('i');
            if (status === 'error') {
                icon.className = 'bi bi-x-circle-fill';
                icon.style.color = 'var(--app-danger)';
            } else {
                icon.className = 'bi bi-check-circle-fill';
                icon.style.color = 'var(--app-success)';
            }
            icon.style.animation = '';
            break;
        }
    }
}

function updateStreamingAgentComplete(agentId, agentName, status, result) {
    const itemId = 'ap-' + agentId.replace(/[^a-zA-Z0-9]/g, '-');
    const item = document.getElementById(itemId);
    if (!item) return;

    // Update status icon in header
    const oldStatus = item.querySelector('.ap-status');
    if (oldStatus) {
        if (status === 'error') {
            oldStatus.className = 'bi bi-x-circle-fill ap-status error';
        } else {
            oldStatus.className = 'bi bi-check-circle-fill ap-status success';
        }
    }

    // Add result summary
    if (result) {
        const detail = document.getElementById(itemId + '-detail');
        if (detail) {
            const truncResult = typeof result === 'string'
                ? (result.length > 200 ? result.substring(0, 200) + '…' : result)
                : JSON.stringify(result).substring(0, 200);
            detail.insertAdjacentHTML('beforeend',
                `<div class="tp-detail-section" style="margin-top: 0.375rem;">Result</div>
                 <div class="tp-detail-code">${escapeHtml(truncResult)}</div>`);
        }
    }

    _updateAgentPanelBadge();
}

async function loadAgentHistory(conversationId) {
    try {
        const resp = await fetch(`/chat/${conversationId}/api/agent-history`);
        const runs = await resp.json();
        if (!runs.length) return;

        const panel = document.getElementById('agent-panel-body');
        const empty = document.getElementById('agent-panel-empty');
        if (empty) empty.style.display = 'none';

        // Update badge
        const badge = document.getElementById('agent-panel-badge');
        if (badge) { badge.textContent = runs.length; badge.style.display = ''; }

        runs.reverse(); // Oldest first
        runs.forEach(run => {
            _addAgentPanelItem(
                run.agent_name, run.agent_id, run.task_description,
                run.status, run.result_text, run.tool_calls_json,
                run.created_at, run.input_tokens, run.output_tokens
            );
        });
    } catch (err) { /* Non-critical */ }
}


/* ==========================================================================
   8. Sidecar Resize
   ========================================================================== */

let _sidecarResizing = false;
let _sidecarStartX = 0;
let _sidecarStartWidth = 0;

function startSidecarResize(event) {
    _sidecarResizing = true;
    _sidecarStartX = event.clientX;
    const panel = document.getElementById('tool-panel');
    _sidecarStartWidth = panel.offsetWidth;
    document.addEventListener('mousemove', doSidecarResize);
    document.addEventListener('mouseup', stopSidecarResize);
    event.preventDefault();
}

function doSidecarResize(event) {
    if (!_sidecarResizing) return;
    const panel = document.getElementById('tool-panel');
    // Dragging left = wider, dragging right = narrower
    const diff = _sidecarStartX - event.clientX;
    const newWidth = Math.max(250, Math.min(800, _sidecarStartWidth + diff));
    panel.style.width = newWidth + 'px';
}

function stopSidecarResize() {
    _sidecarResizing = false;
    document.removeEventListener('mousemove', doSidecarResize);
    document.removeEventListener('mouseup', stopSidecarResize);
    // Save width preference
    const panel = document.getElementById('tool-panel');
    sessionStorage.setItem('spark-sidecar-width', panel.style.width);
}
