/* SparkVoice: text-to-speech with a natural engine and a browser fallback.

   The browser synthesiser is the default and the permanent fallback: if
   ElevenLabs is disabled, unconfigured, over quota, or simply fails, the
   utterance is still spoken. Voice mode never goes silent. */
(function (global) {
    let config = { enabled: false, interaction_mode: 'listen_along', default_voice_id: '' };
    let modeOverride = null;
    let browserVoice = null;
    let speaking = false;
    let currentAudio = null;
    let queue = Promise.resolve();
    const warned = new Set();

    const tts = global.speechSynthesis;

    async function init() {
        try {
            const resp = await fetch('/voice/api/config');
            if (resp.ok) config = await resp.json();
        } catch (err) {
            config = { enabled: false, interaction_mode: 'listen_along', default_voice_id: '' };
        }
        return config;
    }

    function usingElevenLabs() { return !!config.enabled; }
    function interactionMode() { return modeOverride || config.interaction_mode || 'listen_along'; }
    function setInteractionMode(mode) { modeOverride = mode; }
    function setBrowserVoice(voice) { browserVoice = voice; }
    function isSpeaking() { return speaking; }

    function warnOnce(reason) {
        if (warned.has(reason)) return;
        warned.add(reason);
        const messages = {
            bad_key: 'ElevenLabs key was rejected — using the browser voice.',
            quota: 'ElevenLabs quota exhausted — using the browser voice.',
            cap: 'Voice character cap reached — using the browser voice.',
            rate_limited: 'ElevenLabs is rate limiting — using the browser voice.',
            network: 'Could not reach ElevenLabs — using the browser voice.',
            server: 'ElevenLabs is unavailable — using the browser voice.',
        };
        const msg = messages[reason];
        if (msg && global.AppToast) AppToast.warning('Voice', msg);
    }

    // Long text is chunked at sentence boundaries: the browser synthesiser is
    // unreliable on very long utterances.
    function splitIntoSentences(text) {
        const raw = (text || '').match(/[^.!?\n]+[.!?\n]+|[^.!?\n]+$/g) || [text];
        const chunks = [];
        let current = '';
        for (const s of raw) {
            if ((current + s).length > 200 && current) {
                chunks.push(current.trim());
                current = s;
            } else {
                current += s;
            }
        }
        if (current.trim()) chunks.push(current.trim());
        return chunks;
    }

    function speakWithBrowser(text) {
        return new Promise((resolve) => {
            if (!tts) { resolve(); return; }
            const chunks = splitIntoSentences(text);
            let idx = 0;
            function next() {
                if (idx >= chunks.length) { resolve(); return; }
                const utterance = new SpeechSynthesisUtterance(chunks[idx]);
                if (browserVoice) utterance.voice = browserVoice;
                utterance.rate = 1.0;
                utterance.pitch = 1.0;
                utterance.onend = () => { idx++; next(); };
                utterance.onerror = () => resolve();
                tts.speak(utterance);
            }
            next();
        });
    }

    async function speakWithElevenLabs(text, voiceId, conversationId) {
        const resp = await fetch('/voice/api/speak', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text,
                voice_id: voiceId || undefined,
                conversation_id: conversationId || undefined,
            }),
        });
        if (!resp.ok) {
            let reason = 'server';
            try { reason = (await resp.json()).reason || 'server'; } catch (e) { /* keep default */ }
            const err = new Error(reason);
            err.reason = reason;
            throw err;
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        await new Promise((resolve) => {
            const audio = new Audio(url);
            currentAudio = audio;
            audio.onended = resolve;
            audio.onerror = resolve;   // a playback failure must not hang the queue
            audio.play().catch(resolve);
        });
        URL.revokeObjectURL(url);
        currentAudio = null;
    }

    function speak(text, opts) {
        const options = opts || {};
        // Serialise: two agent turns landing together must not talk over each other.
        queue = queue.then(async () => {
            const clean = (text || '').trim();
            if (!clean) return;
            speaking = true;
            try {
                if (usingElevenLabs()) {
                    try {
                        await speakWithElevenLabs(clean, options.voiceId, options.conversationId);
                        return;
                    } catch (err) {
                        warnOnce(err.reason || 'server');
                        // fall through to the browser engine
                    }
                }
                await speakWithBrowser(clean);
            } finally {
                speaking = false;
            }
        }).catch(() => { speaking = false; });
        return queue;
    }

    function cancel() {
        queue = Promise.resolve();
        speaking = false;
        if (tts) { try { tts.cancel(); } catch (e) { /* already stopped */ } }
        if (currentAudio) {
            try { currentAudio.pause(); } catch (e) { /* already stopped */ }
            currentAudio = null;
        }
    }

    global.SparkVoice = {
        init, speak, cancel, isSpeaking, usingElevenLabs,
        interactionMode, setInteractionMode, setBrowserVoice,
    };
})(window);
