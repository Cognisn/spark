/**
 * Spark — Chat message rendering and interaction logic
 * Handles message display, tool call visualization, and UI controls.
 */

const TOOL_RESULTS_MARKER = '[TOOL_RESULTS]';

// Running token totals
let totalTokensIn = 0;
let totalTokensOut = 0;

function updateTokenDisplay(inputTokens, outputTokens) {
    totalTokensIn += inputTokens || 0;
    totalTokensOut += outputTokens || 0;
    const inEl = document.getElementById('tc-in');
    const outEl = document.getElementById('tc-out');
    if (inEl) inEl.textContent = totalTokensIn.toLocaleString();
    if (outEl) outEl.textContent = totalTokensOut.toLocaleString();
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

async function loadChatHistory(conversationId) {
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
        scrollToBottom();
    } catch (err) {
        container.innerHTML = `<div class="alert-app alert-app-danger">Failed to load history.</div>`;
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

    appendTextMessage(role, content, timestamp);
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

function _buildTimestamp(timestamp) {
    return `<div class="message-timestamp">${_formatDateTime(timestamp)}</div>`;
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

function appendTextMessage(role, content, timestamp) {
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

    wrapper.appendChild(bubble);
    wrapper.insertAdjacentHTML('beforeend', _buildTimestamp(timestamp));
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

function _formatToolTime(timestamp) {
    const d = timestamp ? new Date(timestamp) : new Date();
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function _addToolPanelItem(toolName, params, status, result, timestamp) {
    const panel = document.getElementById('tool-panel-body');
    const empty = document.getElementById('tool-panel-empty');
    if (empty) empty.style.display = 'none';

    _toolPanelCount++;
    _updateToolPanelBadge();

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

    panel.appendChild(item);
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
        <div class="approval-actions">
            <button class="btn btn-app-primary btn-sm" onclick="approveAction('${toolUseId}', 'allowed')">
                <i class="bi bi-check-lg me-1"></i>Apply
            </button>
            <button class="btn btn-app-ghost btn-sm" onclick="approveAction('${toolUseId}', 'denied')">
                <i class="bi bi-x-lg me-1"></i>Skip
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

    // Add timestamp outside the bubble
    if (streamingWrapperDiv) {
        streamingWrapperDiv.insertAdjacentHTML('beforeend', _buildTimestamp(null));
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
            badge.className = decision === 'allowed'
                ? 'badge-app badge-app-success'
                : 'badge-app badge-app-danger';
            badge.textContent = decision === 'allowed' ? 'Approved' : 'Rejected';
        }
    } catch (err) {
        AppToast.danger('Error', 'Failed to send response.');
    }
}
