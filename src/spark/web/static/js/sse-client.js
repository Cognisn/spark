/**
 * Spark — Server-Sent Events client for real-time chat streaming
 */

let currentEventSource = null;
let isRequestCancelled = false;
let accumulatedContent = '';
let pendingPermissionRequestId = null;

/**
 * Send a message and stream the response via SSE.
 */
function sendMessageWithSSE(conversationId, message) {
    if (currentEventSource) {
        cancelCurrentRequest();
    }

    isRequestCancelled = false;
    accumulatedContent = '';

    const btn = document.getElementById('btn-send');
    btn.innerHTML = '<i class="bi bi-stop-circle"></i>';
    btn.onclick = cancelCurrentRequest;

    // Show typing indicator
    startStreamingMessage();

    const params = new URLSearchParams({
        message: message,
        conversation_id: conversationId,
    });

    const url = `/stream/chat?${params.toString()}`;
    currentEventSource = new EventSource(url);

    currentEventSource.addEventListener('status', (e) => {
        // Processing indicator — already shown via startStreamingMessage
    });

    currentEventSource.addEventListener('response', (e) => {
        const data = JSON.parse(e.data);
        if (data.final) {
            finaliseStreamingMessage(data.content);
            if (data.usage) {
                updateTokenDisplay(
                    data.usage.input_tokens,
                    data.usage.output_tokens,
                    data.usage.cache_read_input_tokens,
                    data.usage.cache_creation_input_tokens
                );
            }
        } else {
            accumulatedContent += data.content || '';
            updateStreamingMessage(accumulatedContent);
        }
    });

    currentEventSource.addEventListener('tool_start', (e) => {
        const data = JSON.parse(e.data);
        // Finalise the current streaming bubble as a permanent message so the
        // intermediate text ("Sure! Let me check") stays visible in its own bubble.
        // Then reset for the next LLM response after tool completion.
        if (accumulatedContent.trim()) {
            finaliseStreamingMessage(accumulatedContent);
        } else {
            removeStreamingMessage();
        }
        accumulatedContent = '';
        appendStreamingToolStart(data.tool_name, data.params, data.tool_use_id);
    });

    currentEventSource.addEventListener('tool_complete', (e) => {
        const data = JSON.parse(e.data);
        updateStreamingToolComplete(data.tool_use_id, data.tool_name, data.result, data.status);
    });

    currentEventSource.addEventListener('permission_request', (e) => {
        const data = JSON.parse(e.data);
        pendingPermissionRequestId = data.request_id;
        document.getElementById('permission-message').textContent =
            `The tool "${data.tool_name}" is requesting permission to execute.`;
        document.getElementById('permission-details').textContent =
            JSON.stringify(data.params || {}, null, 2);
        new bootstrap.Modal(document.getElementById('permissionModal')).show();
    });

    currentEventSource.addEventListener('compaction_status', (e) => {
        const data = JSON.parse(e.data);
        if (data.status === 'start') {
            appendSystemMessage('Context compaction in progress...');
        } else if (data.status === 'complete') {
            appendSystemMessage(
                `Compaction complete: ${data.original_tokens} → ${data.new_tokens} tokens ` +
                `(${data.messages_rolled_up} messages summarised)`
            );
        }
    });

    currentEventSource.addEventListener('progress', (e) => {
        // Tool iteration progress — could update UI indicator
    });

    currentEventSource.addEventListener('complete', (e) => {
        finaliseStreamingToolGroup();
        closeStream();
    });

    currentEventSource.addEventListener('error', (e) => {
        if (isRequestCancelled) return;

        try {
            const data = JSON.parse(e.data);
            appendTextMessage('error', data.message || 'An error occurred.');
        } catch {
            // EventSource native error (connection lost)
            if (currentEventSource && currentEventSource.readyState === EventSource.CLOSED) {
                appendTextMessage('error', 'Connection lost.');
            }
        }
        closeStream();
    });

    // Handle native EventSource errors (connection failures)
    currentEventSource.onerror = () => {
        if (isRequestCancelled) return;
        if (currentEventSource.readyState === EventSource.CLOSED) {
            closeStream();
        }
    };
}

/**
 * Cancel the current streaming request.
 */
function cancelCurrentRequest() {
    isRequestCancelled = true;
    closeStream();
    finaliseStreamingToolGroup();
    finaliseStreamingMessage(accumulatedContent || '_Request cancelled._');
}

/**
 * Check if a request is currently in progress.
 */
function isRequestInProgress() {
    return currentEventSource !== null;
}

/**
 * Clean up the EventSource and reset UI.
 */
function closeStream() {
    if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
    }
    accumulatedContent = '';

    const btn = document.getElementById('btn-send');
    if (btn) {
        btn.innerHTML = '<i class="bi bi-send"></i>';
        btn.onclick = null;
    }
}
