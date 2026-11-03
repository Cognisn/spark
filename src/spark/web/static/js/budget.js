/**
 * Spark: the monthly token budget banner (A14).
 *
 * From 80% of the budget the page warns; at 100% it says new requests are
 * refused until the budget resets (00:00 UTC on the first of next month).
 * The standing comes from /api/usage/me on load, and from each finished turn.
 * Text only: nothing from the server is ever inserted as HTML.
 */
(function () {
    function format(n) {
        return Number(n).toLocaleString('en-AU');
    }

    function resetDate(iso) {
        return new Date(iso).toLocaleDateString('en-AU', {
            day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
        });
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

    function show(status) {
        if (!status || !status.state) return;
        const el = banner();
        if (status.state === 'warning') {
            el.className = 'alert alert-warning mb-2 py-2 small';
            el.textContent = `You have used ${status.percent}% of your monthly token budget `
                + `(${format(status.used)} of ${format(status.limit)} tokens). `
                + `It resets on ${resetDate(status.resets_at)} (UTC).`;
        } else if (status.state === 'exceeded') {
            el.className = 'alert alert-danger mb-2 py-2 small';
            el.textContent = status.message
                || `You have used your monthly token budget (${format(status.used)} of `
                + `${format(status.limit)} tokens). New requests are refused until it resets on `
                + `${resetDate(status.resets_at)} (UTC).`;
        } else {
            el.className = 'd-none';
            el.textContent = '';
        }
    }

    function refresh() {
        return fetch('/api/usage/me', { headers: { Accept: 'application/json' } })
            .then(r => (r.ok ? r.json() : null))
            .then(show)
            .catch(() => {});
    }

    window.SparkBudget = { show, refresh };
    document.addEventListener('DOMContentLoaded', refresh);
})();
