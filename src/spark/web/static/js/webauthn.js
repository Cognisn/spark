/**
 * Spark: passkey and security key ceremonies (WebAuthn).
 *
 * Every ceremony is a form marked data-webauthn="<kind>" with a
 * data-options-url, a hidden "credential" field and a [data-webauthn-start]
 * button. Pressing the button fetches the options (POST, JSON, the form's own
 * csrf_token in the X-CSRF-Token header, and the form's "token" field where it
 * has one), hands them to the browser (navigator.credentials.create() to
 * register, get() to sign in or verify), puts the browser's answer in the
 * hidden field as JSON and submits the form, so the server answers with an
 * ordinary page. Kinds that register: register, enrol, link, invitation; the
 * others sign in: sign-in, verify.
 *
 * A sign-in form with data-conditional="<selector>" also offers the user's
 * passkeys in the browser's autofill on that field (conditional mediation)
 * where the browser has it; pressing the button cancels that request first.
 *
 * Binary values travel as base64url. Errors are shown in plain words in the
 * form's [data-webauthn-error] element. Nothing here logs: no options,
 * credentials or tokens reach the console.
 */
(function () {
    'use strict';

    const REGISTER = new Set(['register', 'enrol', 'link', 'invitation']);
    const MESSAGES = {
        NotAllowedError:
            'The passkey request was cancelled or timed out. Try again when you are ready.',
        InvalidStateError: 'This passkey or security key is already registered.',
        NotSupportedError: 'This browser or device cannot use a passkey here.',
        SecurityError: 'Passkeys cannot be used on this address.',
        ConstraintError: 'This device cannot confirm it is you. Try another passkey or security key.',
    };
    const FALLBACK = 'The passkey could not be used. Try again.';
    const UNSUPPORTED = 'This browser cannot use passkeys. Use another way to sign in.';

    function supported() {
        return typeof window.PublicKeyCredential === 'function' && !!navigator.credentials;
    }

    // -- base64url --------------------------------------------------------------------

    function toBytes(text) {
        const base64 = String(text).replace(/-/g, '+').replace(/_/g, '/');
        const padded = base64 + '==='.slice((base64.length + 3) % 4);
        const raw = atob(padded);
        const bytes = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i += 1) {
            bytes[i] = raw.charCodeAt(i);
        }
        return bytes.buffer;
    }

    function toText(buffer) {
        const bytes = new Uint8Array(buffer);
        let raw = '';
        for (let i = 0; i < bytes.length; i += 1) {
            raw += String.fromCharCode(bytes[i]);
        }
        return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }

    function descriptors(list) {
        return (list || []).map(function (d) {
            return Object.assign({}, d, { id: toBytes(d.id) });
        });
    }

    // -- Options (JSON to the browser's types) --------------------------------------------

    function creationOptions(json) {
        if (typeof PublicKeyCredential.parseCreationOptionsFromJSON === 'function') {
            return PublicKeyCredential.parseCreationOptionsFromJSON(json);
        }
        const options = Object.assign({}, json, {
            challenge: toBytes(json.challenge),
            user: Object.assign({}, json.user, { id: toBytes(json.user.id) }),
        });
        if (json.excludeCredentials) {
            options.excludeCredentials = descriptors(json.excludeCredentials);
        }
        return options;
    }

    function requestOptions(json) {
        if (typeof PublicKeyCredential.parseRequestOptionsFromJSON === 'function') {
            return PublicKeyCredential.parseRequestOptionsFromJSON(json);
        }
        const options = Object.assign({}, json, { challenge: toBytes(json.challenge) });
        if (json.allowCredentials) {
            options.allowCredentials = descriptors(json.allowCredentials);
        }
        return options;
    }

    // -- The browser's answer to JSON ------------------------------------------------------

    function answerJSON(credential) {
        if (typeof credential.toJSON === 'function') {
            try {
                return credential.toJSON();
            } catch (e) {
                // Some browsers expose toJSON but fail on extension results: build it by hand.
            }
        }
        const response = credential.response;
        const json = {
            id: credential.id,
            rawId: toText(credential.rawId),
            type: credential.type,
            authenticatorAttachment: credential.authenticatorAttachment || null,
            clientExtensionResults: credential.getClientExtensionResults
                ? credential.getClientExtensionResults()
                : {},
            response: { clientDataJSON: toText(response.clientDataJSON) },
        };
        if (response.attestationObject) {
            json.response.attestationObject = toText(response.attestationObject);
            if (typeof response.getTransports === 'function') {
                json.response.transports = response.getTransports();
            }
        } else {
            json.response.authenticatorData = toText(response.authenticatorData);
            json.response.signature = toText(response.signature);
            json.response.userHandle = response.userHandle ? toText(response.userHandle) : null;
        }
        return json;
    }

    // -- One form --------------------------------------------------------------------------

    function field(form, name) {
        const input = form.querySelector('input[name="' + name + '"]');
        return input ? input.value : '';
    }

    function showError(form, text) {
        const box = form.querySelector('[data-webauthn-error]');
        if (!box) {
            return;
        }
        box.textContent = text || '';
        box.hidden = !text;
        if (text) {
            box.setAttribute('role', 'alert');
        }
    }

    function busy(form, on) {
        const button = form.querySelector('[data-webauthn-start]');
        if (button) {
            button.disabled = on;
        }
    }

    async function fetchOptions(form) {
        const body = {};
        const token = field(form, 'token');
        if (token) {
            body.token = token;
        }
        const response = await fetch(form.dataset.optionsUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                Accept: 'application/json',
                'X-CSRF-Token': field(form, 'csrf_token'),
            },
            body: JSON.stringify(body),
        });
        let payload = null;
        try {
            payload = await response.json();
        } catch (e) {
            payload = null;
        }
        if (!response.ok || !payload || !payload.publicKey) {
            const detail = payload && typeof payload.detail === 'string' ? payload.detail : FALLBACK;
            const error = new Error(detail);
            error.name = 'SparkRefusal';
            throw error;
        }
        return payload.publicKey;
    }

    function submitWith(form, credential) {
        form.querySelector('input[name="credential"]').value = JSON.stringify(
            answerJSON(credential)
        );
        form.submit();
    }

    function message(error) {
        if (!error) {
            return FALLBACK;
        }
        if (error.name === 'SparkRefusal') {
            return error.message;
        }
        return MESSAGES[error.name] || FALLBACK;
    }

    async function run(form, conditional) {
        const kind = form.dataset.webauthn;
        const json = await fetchOptions(form);
        let credential;
        if (REGISTER.has(kind)) {
            credential = await navigator.credentials.create({ publicKey: creationOptions(json) });
        } else {
            const request = { publicKey: requestOptions(json) };
            if (conditional) {
                request.mediation = 'conditional';
                request.signal = conditional.signal;
            }
            credential = await navigator.credentials.get(request);
        }
        if (credential) {
            submitWith(form, credential);
        }
    }

    async function startConditional(form, state) {
        const selector = form.dataset.conditional;
        if (!selector || !document.querySelector(selector)) {
            return;
        }
        const available =
            typeof PublicKeyCredential.isConditionalMediationAvailable === 'function' &&
            (await PublicKeyCredential.isConditionalMediationAvailable());
        if (!available) {
            return;
        }
        state.controller = new AbortController();
        try {
            await run(form, state.controller);
        } catch (error) {
            // Cancelled for the button, or the user went another way: nothing to say.
            if (error && error.name !== 'AbortError' && error.name !== 'NotAllowedError') {
                showError(form, message(error));
            }
        }
    }

    function wire(form) {
        const button = form.querySelector('[data-webauthn-start]');
        if (!supported()) {
            showError(form, UNSUPPORTED);
            if (button) {
                button.disabled = true;
            }
            return;
        }
        const state = { controller: null };
        if (button) {
            button.addEventListener('click', async function () {
                if (state.controller) {
                    state.controller.abort();
                    state.controller = null;
                }
                showError(form, '');
                busy(form, true);
                try {
                    await run(form, null);
                } catch (error) {
                    showError(form, message(error));
                    busy(form, false);
                }
            });
        }
        startConditional(form, state);
    }

    function init() {
        document.querySelectorAll('form[data-webauthn]').forEach(function (form) {
            if (form.dataset.webauthnWired) {
                return;
            }
            form.dataset.webauthnWired = 'yes';
            wire(form);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
