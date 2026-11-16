/**
 * Spark: the monthly credit banner (M6, M11).
 *
 * Credits are counted per connection. From 80% of the user's allowance on a
 * connection the page warns; at 100% it says new requests on that connection are
 * refused until the credits reset (00:00 UTC on the first of next month).
 * Unmetered connections and personal mode never show a banner.
 *
 * The standing comes from /api/usage/me on load, and from each finished turn. A
 * page about one connection (a chat, on its conversation's model) asks about that
 * connection with SparkBudget.setConnection(id); other pages (debates, panels)
 * show the user's most used metered connection.
 * Text only: nothing from the server is ever inserted as HTML.
 */
(function () {
    let preferred = null;

    function amount(n) {
        const value = Number(n);
        return value.toLocaleString('en-AU', {
            minimumFractionDigits: Number.isInteger(value) ? 0 : 2,
            maximumFractionDigits: 2,
        });
    }

    function resetDate(iso) {
        return new Date(iso).toLocaleDateString('en-AU', {
            day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
        });
    }

    function connectionOf(modelId) {
        const text = String(modelId || '');
        const slash = text.indexOf('/');
        return slash > 0 ? text.slice(0, slash) : null;
    }

    function banner() {
        let el = document.getElementById('budget-banner');
        if (!el) {
            el = document.createElement('div');
            el.id = 'budget-banner';
            el.setAttribute('role', 'status');
            const anchor = document.querySelector('[data-budget-anchor]');
            if (anchor) anchor.prepend(el); else document.body.prepend(el);
        }
        return el;
    }

    function show(status, fromRefresh) {
        if (!status || !status.state) return;
        // A turn reports the user's most used connection; a page about another
        // connection asks about its own instead.
        if (!fromRefresh && preferred && status.connection_id && status.connection_id !== preferred) {
            refresh();
            return;
        }
        const el = banner();
        const name = status.connection || status.connection_id || 'this connection';
        if (status.state === 'warning') {
            el.className = 'alert alert-warning mb-2 py-2 small';
            el.textContent = `You have used ${status.percent}% of your monthly credits on ${name} `
                + `(${amount(status.used)} of ${amount(status.limit)} credits). `
                + `They reset on ${resetDate(status.resets_at)} (UTC).`;
        } else if (status.state === 'exceeded') {
            el.className = 'alert alert-danger mb-2 py-2 small';
            el.textContent = status.message
                || `You have used your monthly credits on ${name} (${amount(status.used)} of `
                + `${amount(status.limit)} credits). New requests on ${name} are refused until `
                + `they reset on ${resetDate(status.resets_at)} (UTC).`;
        } else {
            el.className = 'd-none';
            el.textContent = '';
        }
    }

    function refresh() {
        const query = preferred ? `?connection=${encodeURIComponent(preferred)}` : '';
        return fetch(`/api/usage/me${query}`, { headers: { Accept: 'application/json' } })
            .then(r => (r.ok ? r.json() : null))
            .then(status => show(status, true))
            .catch(() => {});
    }

    function setConnection(connectionId) {
        preferred = connectionId || null;
        return refresh();
    }

    window.SparkBudget = { show, refresh, setConnection, connectionOf, amount };
    document.addEventListener('DOMContentLoaded', () => {
        const anchor = document.querySelector('[data-budget-connection]');
        preferred = (anchor && anchor.dataset.budgetConnection)
            || connectionOf(window._currentModelId) || null;
        refresh();
    });
})();
