/**
 * Spark — Server-Sent Events client for real-time chat streaming
 */

let currentEventSource = null;
let isRequestCancelled = false;
let accumulatedContent = '';
let pendingPermissionRequestId = null;
let currentStreamId = null;

/**
 * Send a message and stream the response via SSE.
 */
function sendMessageWithSSE(conversationId, message, attachmentIds) {
    if (currentEventSource) {
        cancelCurrentRequest();
    }

    isRequestCancelled = false;
    accumulatedContent = '';

    const btn = document.getElementById('btn-send');
    btn.innerHTML = '<i class="bi bi-stop-circle" aria-hidden="true"></i>';
    btn.title = 'Stop';
    btn.setAttribute('aria-label', 'Stop the response');
    btn.onclick = cancelCurrentRequest;

    // Show the working indicator ("Thinking...") at once
    startStreamingMessage();
    chatWorking().begin();

    const params = new URLSearchParams({
        message: message,
        conversation_id: conversationId,
    });
    // Files uploaded in the composer: the server names them in the message by id.
    if (attachmentIds && attachmentIds.length) params.set('attachments', attachmentIds.join(','));

    const url = `/stream/chat?${params.toString()}`;
    // A server-mode session is refreshed first: an EventSource cannot read a 401.
    const ready = window.SparkSession ? window.SparkSession.ensure() : Promise.resolve(true);
    ready.then(() => {
        if (!isRequestCancelled) openChatStream(url);
    });
}

/**
 * Open the chat stream and attach its handlers.
 */
function openChatStream(url) {
    currentEventSource = new EventSource(url);

    // The sender was removed from the conversation: their turn is cancelled.
    currentEventSource.addEventListener('removed', () => {
        settleStreamingMessage();
        closeStream();
        if (window.SparkSharing) SparkSharing._onEvent('removed', {});
    });

    currentEventSource.addEventListener('status', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.stream_id) currentStreamId = data.stream_id;
        } catch (err) {
            // Ignore — old servers without stream_id still work.
        }
    });

    currentEventSource.addEventListener('response', (e) => {
        const data = JSON.parse(e.data);
        if (data.final) {
            finaliseStreamingMessage(data.content);
            if (data.budget && window.SparkBudget) SparkBudget.show(data.budget);
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

    // The model's reasoning: shown above the reply as it arrives, never as part of it.
    currentEventSource.addEventListener('reasoning', (e) => {
        const data = JSON.parse(e.data);
        addStreamingReasoning(data.text || '');
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
        updateStreamingToolComplete(data.tool_use_id, data.tool_name, data.result, data.status, data.inspection);
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
        chatWorking().compaction(data.status === 'start');
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

    currentEventSource.addEventListener('agent_model_approval', (e) => {
        const data = JSON.parse(e.data);
        showAgentModelApproval(data);
    });

    currentEventSource.addEventListener('agent_start', (e) => {
        const data = JSON.parse(e.data);
        appendStreamingAgentStart(data.agent_name, data.agent_id, data.task, data.model_id);
    });

    currentEventSource.addEventListener('agent_tool_call', (e) => {
        const data = JSON.parse(e.data);
        updateStreamingAgentToolCall(data.agent_id, data.tool_name, data.params);
    });

    currentEventSource.addEventListener('agent_tool_result', (e) => {
        const data = JSON.parse(e.data);
        updateStreamingAgentToolResult(data.agent_id, data.tool_name, data.result, data.status, data.inspection);
    });

    currentEventSource.addEventListener('agent_complete', (e) => {
        const data = JSON.parse(e.data);
        updateStreamingAgentComplete(data.agent_id, data.agent_name, data.status, data.result);
    });

    // Refused at 100% of the monthly token budget: nothing of the turn was stored.
    currentEventSource.addEventListener('budget_exceeded', (e) => {
        const data = JSON.parse(e.data);
        if (window.SparkBudget) SparkBudget.show(data);
        appendTextMessage('error', data.message || 'Your monthly credits on this connection are used.');
        settleStreamingMessage();
        closeStream();
    });

    currentEventSource.addEventListener('cancelled', (e) => {
        appendSystemMessage('Turn cancelled by user.');
    });

    currentEventSource.addEventListener('complete', (e) => {
        finaliseStreamingToolGroup();
        settleStreamingMessage();
        closeStream();
    });

    currentEventSource.addEventListener('error', (e) => {
        if (isRequestCancelled) return;

        try {
            const data = JSON.parse(e.data);
            appendTextMessage('error', data.message || 'An error occurred.');
        } catch {
            // EventSource native error (connection lost): the stream is closed below rather
            // than left to reconnect, which would send the message again, so the page says so
            // instead of leaving a turn that looks as if it were still working
            if (currentEventSource) appendTextMessage('error', 'Connection lost.');
            settleStreamingMessage();
            closeStream();
            // The turn runs on without this stream: the live view attaches to it again
            if (window.SparkSharing && SparkSharing.reattach) SparkSharing.reattach();
            return;
        }
        settleStreamingMessage();
        closeStream();
    });

    // Handle native EventSource errors (connection failures)
    currentEventSource.onerror = () => {
        // A server-sent error event ends the stream first: nothing is left to look at
        if (isRequestCancelled || !currentEventSource) return;
        if (currentEventSource.readyState === EventSource.CLOSED) {
            // Refused before it started (another participant's turn, a read-only role,
            // a model the sender may not use): the page says why and nothing was sent.
            const refused = !currentStreamId;
            closeStream();
            if (refused) {
                removeStreamingMessage();
                appendSystemMessage('Your message was not sent.');
                if (window.SparkSharing) SparkSharing.refresh();
            }
        }
    };
}

/**
 * A turn that ends without a final reply (an error, a removal, tool results only) leaves
 * no empty bubble behind: text that arrived is kept, and the rest is removed.
 */
function settleStreamingMessage() {
    if (!streamingMessageDiv) return;
    if (accumulatedContent.trim()) finaliseStreamingMessage(accumulatedContent);
    else removeStreamingMessage();
}

/**
 * Cancel the current streaming request.
 */
function cancelCurrentRequest() {
    isRequestCancelled = true;
    // Fire-and-forget server cancel. If it fails, fall through to client-side close.
    if (currentStreamId) {
        try {
            fetch('/stream/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                // The conversation lets a replica that does not run the turn pass it on
                body: JSON.stringify({
                    stream_id: currentStreamId,
                    conversation_id: typeof conversationId !== 'undefined' ? conversationId : 0,
                }),
            }).catch(() => {});
        } catch (e) {
            // Best-effort
        }
    }
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
    currentStreamId = null;
    accumulatedContent = '';
    // The turn is over, however it ended (finished, stopped or failed)
    chatWorking().end();

    const btn = document.getElementById('btn-send');
    if (btn) {
        btn.innerHTML = '<i class="bi bi-send" aria-hidden="true"></i>';
        btn.title = 'Send';
        btn.setAttribute('aria-label', 'Send');
        btn.onclick = null;
    }
}
