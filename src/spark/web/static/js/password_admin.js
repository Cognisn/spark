// Password administration: importing a file of hashes into a watch list.
//
// The chosen file is sent as the raw request body, so a large file streams to the server a line
// at a time instead of being read into the page or parsed as a form. The page's CSRF token goes
// in the header (csrf.js wraps fetch to add it). The answer is JSON: ok and a message.
(function () {
    'use strict';

    function show(box, ok, text) {
        box.hidden = false;
        box.className = 'alert-app mb-3 ' + (ok ? 'alert-app-success' : 'alert-app-danger');
        box.textContent = text;
    }

    document.querySelectorAll('form[data-import]').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var box = document.getElementById('import-result');
            var file = form.querySelector('input[type="file"]').files[0];
            var threshold = form.querySelector('input[type="number"]').value.trim();
            if (!file) {
                return;
            }
            var button = form.querySelector('button[type="submit"]');
            button.disabled = true;
            var url = form.dataset.import + (threshold ? '?threshold=' + encodeURIComponent(threshold) : '');
            fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/octet-stream', 'Accept': 'application/json' },
                body: file,
            }).then(function (response) {
                return response.json().catch(function () { return { ok: false, message: 'The import was refused.' }; });
            }).then(function (answer) {
                if (answer.ok) {
                    show(box, true, answer.message + ' ' + answer.imported + ' hashes kept' +
                        (answer.below_threshold ? ', ' + answer.below_threshold + ' below the threshold' : '') + '.');
                    var count = document.getElementById('entry-count');
                    if (count) { count.textContent = answer.imported; }
                } else {
                    show(box, false, answer.message || 'The import was refused.');
                }
            }).catch(function () {
                show(box, false, 'The import could not be sent. Check the connection and try again.');
            }).then(function () {
                button.disabled = false;
            });
        });
    });
})();
