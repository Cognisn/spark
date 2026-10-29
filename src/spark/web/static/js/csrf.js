/**
 * Spark: send the page's CSRF token on every state-changing request.
 *
 * Loaded before any other script. Wraps window.fetch so that every same-origin
 * POST, PUT, PATCH and DELETE carries the token from
 * <meta name="csrf-token"> in the X-CSRF-Token header. The token is read at
 * call time and never sent to another origin. HTML forms carry it as a hidden
 * csrf_token field instead.
 */
(function () {
    'use strict';

    const HEADER = 'X-CSRF-Token';
    const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
    const originalFetch = window.fetch;

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

    window.fetch = function (input, init) {
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
    };
})();
