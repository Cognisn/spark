// Audit trail: View opens the formatted details under a row; Copy copies them.
(function () {
    function announcer() {
        let live = document.getElementById('audit-live');
        if (!live) {
            live = document.createElement('div');
            live.id = 'audit-live';
            live.className = 'visually-hidden';
            live.setAttribute('role', 'status');
            live.setAttribute('aria-live', 'polite');
            document.body.appendChild(live);
        }
        return live;
    }

    document.querySelectorAll('[data-audit-view]').forEach((button) => {
        button.addEventListener('click', () => {
            const panel = document.getElementById(button.getAttribute('aria-controls'));
            if (!panel) { return; }
            const open = button.getAttribute('aria-expanded') !== 'true';
            button.setAttribute('aria-expanded', open ? 'true' : 'false');
            button.textContent = open ? 'Hide' : 'View';
            panel.hidden = !open;
        });
    });

    document.querySelectorAll('[data-audit-copy]').forEach((button) => {
        button.addEventListener('click', async () => {
            const pre = button.closest('.audit-details').querySelector('pre');
            const live = announcer();
            try {
                await navigator.clipboard.writeText(pre.textContent);
                button.textContent = 'Copied';
                live.textContent = 'Details copied to the clipboard.';
            } catch (error) {
                live.textContent = 'The details could not be copied.';
            }
            setTimeout(() => { button.textContent = 'Copy'; }, 2000);
        });
    });
})();
