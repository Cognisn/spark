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
        appendToolCallGroup(toolCalls);
    }
    // If only tool_use blocks with no names (malformed), skip — don't render empty group
}


/* ==========================================================================
   3. Tool Call Display (Termograph-style)
   ========================================================================== */

function appendToolCallGroup(toolCalls, statuses) {
    if (!toolCalls || !toolCalls.length) return;

    const container = document.getElementById('chat-messages');
    const groupId = 'tcg-' + Math.random().toString(36).substr(2, 8);

    const completedCount = toolCalls.length;
    const statusText = `${completedCount} tool call${completedCount !== 1 ? 's' : ''} completed`;

    const group = document.createElement('div');
    group.className = 'tool-call-group';
    group.innerHTML = `
        <div class="group-header" onclick="toggleToolGroup('${groupId}')">
            <div class="group-summary">
                <i class="bi bi-tools" style="color: var(--app-accent);"></i>
                <span class="tool-count">${statusText}</span>
                <div class="status-dots">
                    ${toolCalls.slice(0, 8).map(() =>
                        `<span class="dot success"></span>`
                    ).join('')}
                    ${toolCalls.length > 8 ? `<span style="font-size:0.6875rem;color:var(--app-text-muted);">+${toolCalls.length - 8}</span>` : ''}
                </div>
            </div>
            <i class="bi bi-chevron-down" id="${groupId}-chevron"
               style="color: var(--app-text-muted); transition: transform 0.2s;"></i>
        </div>
        <div class="group-body" id="${groupId}-body" style="display: none;">
            ${toolCalls.map(tc => buildActionCard(tc)).join('')}
        </div>
    `;

    container.appendChild(group);
}

function buildActionCard(toolCall, status = 'success', result = null) {
    const cardId = 'ac-' + Math.random().toString(36).substr(2, 8);
    const statusIcon = {
        running: '<i class="bi bi-arrow-repeat tool-status running"></i>',
        success: '<i class="bi bi-check-circle-fill tool-status success"></i>',
        error: '<i class="bi bi-x-circle-fill tool-status error"></i>',
    }[status] || '';

    const toolIcon = getToolIcon(toolCall.name || '');
    const resultText = result
        ? (typeof result === 'string' ? result.trim() : JSON.stringify(result, null, 2))
        : null;

    let detailHtml = '';
    if (resultText && resultText.length > 0) {
        detailHtml = `
            <div class="action-detail" id="${cardId}-detail" style="display: none;">
                <div class="action-detail-label">Result:</div>
                <div class="action-detail-code">${escapeHtml(resultText)}</div>
            </div>`;
    } else {
        const paramsJson = JSON.stringify(toolCall.input || {}, null, 2);
        if (paramsJson !== '{}' && paramsJson !== '""' && paramsJson.length > 2) {
            detailHtml = `
                <div class="action-detail" id="${cardId}-detail" style="display: none;">
                    <div class="action-detail-label">Parameters:</div>
                    <div class="action-detail-code">${escapeHtml(paramsJson)}</div>
                </div>`;
        }
    }

    return `
        <div class="action-card">
            <div class="action-header" onclick="toggleActionDetail('${cardId}')">
                <i class="bi ${toolIcon} tool-icon"></i>
                <span class="tool-name">${escapeHtml(toolCall.name || 'Unknown')}</span>
                ${statusIcon}
                <i class="bi bi-chevron-down chevron-icon" id="${cardId}-chevron"></i>
            </div>
            ${detailHtml}
        </div>
    `;
}

function appendToolResults(results) {
    // Tool results are already represented in the tool call group from the
    // preceding assistant message. Don't render them again as separate cards.
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

let streamingToolGroup = null;
let streamingToolCount = 0;

function _ensureStreamingToolGroup() {
    if (streamingToolGroup) return;
    const container = document.getElementById('chat-messages');
    streamingToolGroup = document.createElement('div');
    streamingToolGroup.className = 'tool-call-group';
    streamingToolGroup.id = 'streaming-tool-group';
    streamingToolCount = 0;
    streamingToolGroup.innerHTML = `
        <div class="group-header" onclick="toggleToolGroup('streaming-tg')">
            <div class="group-summary">
                <i class="bi bi-tools" style="color: var(--app-accent);"></i>
                <span class="tool-count" id="streaming-tg-count">Running tools...</span>
                <div class="status-dots" id="streaming-tg-dots"></div>
            </div>
            <i class="bi bi-chevron-down" id="streaming-tg-chevron"
               style="color: var(--app-text-muted); transition: transform 0.2s;"></i>
        </div>
        <div class="group-body" id="streaming-tg-body" style="display: none;"></div>
    `;
    container.appendChild(streamingToolGroup);
    scrollToBottom();
}

function appendStreamingToolStart(toolName, params, toolUseId) {
    _ensureStreamingToolGroup();
    streamingToolCount++;
    const body = document.getElementById('streaming-tg-body');
    const card = document.createElement('div');
    card.id = `tool-${toolUseId}`;
    card.innerHTML = buildActionCard({ name: toolName, input: params }, 'running');
    body.appendChild(card);

    // Update dots
    const dots = document.getElementById('streaming-tg-dots');
    dots.innerHTML += '<span class="dot running"></span>';

    // Update count text
    document.getElementById('streaming-tg-count').textContent = `Running tools...`;
    scrollToBottom();
}

function updateStreamingToolComplete(toolUseId, toolName, result, status) {
    const existing = document.getElementById(`tool-${toolUseId}`);
    if (existing) {
        existing.innerHTML = buildActionCard({ name: toolName, input: {} }, status, result);
    }

    // Update the corresponding dot
    const dots = document.getElementById('streaming-tg-dots');
    if (dots) {
        const runningDot = dots.querySelector('.dot.running');
        if (runningDot) {
            runningDot.className = `dot ${status}`;
        }
    }
}

function finaliseStreamingToolGroup() {
    if (!streamingToolGroup) return;
    const countEl = document.getElementById('streaming-tg-count');
    if (countEl) {
        countEl.textContent = `${streamingToolCount} tool call${streamingToolCount !== 1 ? 's' : ''} completed`;
    }
    // Collapse the body by default now that it's done
    const body = document.getElementById('streaming-tg-body');
    if (body) body.style.display = 'none';
    streamingToolGroup = null;
    streamingToolCount = 0;
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
