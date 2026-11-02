/**
 * Spark: send the page's CSRF token on every state-changing request, and keep
 * a server-mode session alive.
 *
 * Loaded before any other script. Wraps window.fetch so that every same-origin
 * POST, PUT, PATCH and DELETE carries the token from
 * <meta name="csrf-token"> in the X-CSRF-Token header. The token is read at
 * call time and never sent to another origin. HTML forms carry it as a hidden
 * csrf_token field instead.
 *
 * Server mode (warden): a same-origin request answered 401 with
 * "X-Spark-Session: refresh" has an expired access token. The wrapper posts
 * once to the refresh path (the refresh cookie is sent there only), then
 * retries the request. Refreshes are serialised: one in flight per tab, and
 * across tabs through the Web Locks API where the browser has it. A refused
 * refresh, or "X-Spark-Session: sign-in", sends the page to the sign-in screen.
 * window.SparkSession.ensure() refreshes if needed before an EventSource opens
 * (EventSource cannot read a 401).
 */
(function () {
    'use strict';

    const HEADER = 'X-CSRF-Token';
    const SESSION = 'X-Spark-Session';
    const REFRESH_PATH = '/auth/session/refresh';
    const SESSION_PATH = '/auth/session';
    const SIGN_IN_PATH = '/auth/sign-in';
    const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
    const originalFetch = window.fetch;
    let refreshing = null;

    function token() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') || '' : '';
    }

    function isRequest(input) {
        return typeof Request !== 'undefined' && input instanceof Request;
    }

    function sameOrigin(input) {
        const raw = isRequest(input) ? input.url : String(input);
        try {
            return new URL(raw, location.href).origin === location.origin;
        } catch (e) {
            return false;
        }
    }

    function send(input, init) {
        const method = String(
            (init && init.method) || (isRequest(input) ? input.method : 'GET')
        ).toUpperCase();
        if (!UNSAFE.has(method) || !sameOrigin(input)) {
            return originalFetch(input, init);
        }
        const headers = new Headers(
            (init && init.headers) || (isRequest(input) ? input.headers : undefined)
        );
        headers.set(HEADER, token());
        return originalFetch(input, Object.assign({}, init, { headers: headers }));
    }

    function sessionAnswer(response) {
        if (!response || response.status !== 401 || !response.headers ||
            typeof response.headers.get !== 'function') {
            return null;
        }
        return response.headers.get(SESSION);
    }

    function signIn() {
        const next = location.pathname + (location.search || '');
        location.assign(SIGN_IN_PATH + '?next=' + encodeURIComponent(next));
    }

    function postRefresh() {
        const headers = new Headers();
        headers.set(HEADER, token());
        return originalFetch(REFRESH_PATH, {
            method: 'POST',
            credentials: 'same-origin',
            headers: headers,
        }).then(function (response) {
            return Boolean(response && response.status === 204);
        }, function () {
            return false;
        });
    }

    function refreshSession() {
        if (!refreshing) {
            const locks = typeof navigator !== 'undefined' && navigator.locks;
            const run = locks && typeof locks.request === 'function'
                ? locks.request('spark-session-refresh', postRefresh)
                : postRefresh();
            refreshing = Promise.resolve(run).then(function (ok) {
                refreshing = null;
                return ok;
            });
        }
        return refreshing;
    }

    window.fetch = function (input, init) {
        if (!sameOrigin(input)) {
            return originalFetch(input, init);
        }
        const retry = isRequest(input) ? input.clone() : input;
        return send(input, init).then(function (response) {
            const answer = sessionAnswer(response);
            if (answer === 'refresh') {
                return refreshSession().then(function (ok) {
                    if (ok) {
                        return send(retry, init);
                    }
                    signIn();
                    return response;
                });
            }
            if (answer === 'sign-in') {
                signIn();
            }
            return response;
        });
    };

    window.SparkSession = {
        refresh: refreshSession,
        /** Resolve once the session is usable (refreshing it if needed). */
        ensure: function () {
            return window.fetch(SESSION_PATH, { headers: { Accept: 'application/json' } })
                .then(function (response) {
                    return Boolean(response && response.status !== 401);
                }, function () {
                    return false;
                });
        },
    };
})();
