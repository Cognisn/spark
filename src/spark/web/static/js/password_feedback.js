// Live password feedback (CS23): a checklist under the new-password field, ticked while the user types.
//
// The rules come from warden's policy description for this place; the evaluator is warden's own
// WardenPasswordRules (served by this app from the installed package). The browser ticks the list
// at once; a short moment after typing stops, the server checks the candidate too, which adds what
// the browser cannot know (the user's own details, the watch lists, the breach range service) and
// replaces the ticks, because the server is the authority. Nothing is stored: the password is sent
// only to this app's own check route, over the page's own session or token.
(function () {
    'use strict';

    var DEBOUNCE_MS = 350;
    var ICONS = { met: 'bi-check-circle-fill', waived: 'bi-dash-circle-fill', unmet: 'bi-x-circle', pending: 'bi-circle' };
    var SPOKEN = { met: 'Met: ', waived: 'Not needed: ', unmet: 'Not met: ', pending: 'Not yet checked: ' };

    function csrfOf(root) {
        var form = root.closest('form') || document.querySelector('form');
        var field = form ? form.querySelector('input[name="csrf_token"]') : null;
        return field ? field.value : '';
    }

    function post(root, url, payload) {
        var body = Object.assign({}, payload);
        if (root.dataset.token) {
            body.token = root.dataset.token;
        }
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfOf(root) },
            body: JSON.stringify(body),
        }).then(function (response) {
            if (!response.ok) {
                throw new Error('unavailable');
            }
            return response.json();
        });
    }

    function codesOf(rules) {
        var codes = ['min_length', 'max_length'];
        if (rules.min_classes > 0) { codes.push('min_classes'); }
        if (rules.max_repeat !== null) { codes.push('max_repeat'); }
        if (rules.forbid_user_details) { codes.push('no_email', 'no_display_name'); }
        return codes;
    }

    function setLine(node, text) {
        node.textContent = text || '';
        node.hidden = !text;
    }

    function setUp(root) {
        var input = document.querySelector(root.dataset.input);
        var list = root.querySelector('.password-feedback-list');
        var note = root.querySelector('.password-feedback-note');
        var reason = root.querySelector('.password-feedback-reason');
        var lines = {
            passphrase: root.querySelector('#password-feedback-passphrase'),
            tier: root.querySelector('#password-feedback-tier'),
            history: root.querySelector('#password-feedback-history'),
        };
        var rules = null;
        var items = {};
        var timer = null;
        var sequence = 0;

        if (!input || !window.WardenPasswordRules) {
            return;
        }

        // The Create user form describes the policy for the roles ticked, and has no server check
        // (there is no credential of the new person to bind it to); the server still decides on save.
        function chosenRoles() {
            return Array.prototype.filter.call(document.querySelectorAll(root.dataset.roles), function (box) {
                return box.checked;
            }).map(function (box) { return box.value; });
        }

        function mark(code, state) {
            var item = items[code];
            if (!item) { return; }
            item.dataset.state = state;
            item.querySelector('i').className = 'bi ' + ICONS[state];
            item.querySelector('.visually-hidden').textContent = SPOKEN[state];
        }

        function evaluateNow() {
            if (!rules) { return; }
            var evaluation = window.WardenPasswordRules.evaluate(input.value, rules, null);
            var detail = {};
            evaluation.results.forEach(function (result) { detail[result.code] = result; });
            Object.keys(items).forEach(function (code) {
                var result = detail[code];
                if (!result || !input.value) {
                    mark(code, 'pending');
                } else {
                    mark(code, result.waived ? 'waived' : (result.passed ? 'met' : 'unmet'));
                }
            });
            setLine(lines.passphrase, input.value ? window.WardenPasswordRules.passphraseLine(rules, evaluation) : null);
            setLine(lines.tier, input.value ? window.WardenPasswordRules.tierLine(rules, evaluation) : null);
        }

        function draw(policy) {
            rules = policy.rules;
            list.textContent = '';
            items = {};
            codesOf(rules).forEach(function (code) {
                var item = document.createElement('li');
                item.dataset.code = code;
                var icon = document.createElement('i');
                icon.setAttribute('aria-hidden', 'true');
                var spoken = document.createElement('span');
                spoken.className = 'visually-hidden';
                var text = document.createElement('span');
                text.className = 'password-feedback-text';
                text.textContent = window.WardenPasswordRules.ruleLabel(code, rules);
                item.append(icon, ' ', spoken, text);
                list.appendChild(item);
                items[code] = item;
                mark(code, 'pending');
            });
            if (policy.history_count > 0 && !root.dataset.roles) {
                var which = policy.history_count === 1 ? 'your last password' : 'any of your last ' + policy.history_count + ' passwords';
                setLine(lines.history, 'It must not be the same as ' + which + '. Spark checks this when you save.');
            }
            note.textContent = 'Each line ticks as your new password meets it. Spark checks the password again when you save it.';
            evaluateNow();
        }

        function applyServer(answer) {
            (answer.results || []).forEach(function (result) {
                mark(result.code, result.waived ? 'waived' : (result.passed ? 'met' : 'unmet'));
            });
            if (answer.watch_list && answer.watch_list.reason) {
                setLine(reason, answer.watch_list.reason + '. Choose another password.');
                reason.dataset.state = 'blocked';
            } else if (answer.watch_list_unavailable) {
                setLine(reason, 'The list of breached passwords could not be reached just now. Spark checks again when you save.');
                reason.dataset.state = 'unavailable';
            } else {
                setLine(reason, '');
            }
        }

        function checkOnServer() {
            var mine = ++sequence;
            if (!root.dataset.checkUrl) {
                return;
            }
            if (!input.value) {
                setLine(reason, '');
                return;
            }
            post(root, root.dataset.checkUrl, { password: input.value }).then(function (answer) {
                if (mine === sequence) { applyServer(answer); }
            }).catch(function () {
                if (mine === sequence) { setLine(reason, ''); }
            });
        }

        input.addEventListener('input', function () {
            evaluateNow();
            window.clearTimeout(timer);
            timer = window.setTimeout(checkOnServer, DEBOUNCE_MS);
        });

        function load() {
            var payload = root.dataset.roles ? { roles: chosenRoles() } : {};
            post(root, root.dataset.policyUrl, payload).then(draw).catch(function () {
                note.textContent = 'Spark checks your password when you save it, and says what it needs.';
            });
        }

        if (root.dataset.roles) {
            document.querySelectorAll(root.dataset.roles).forEach(function (box) {
                box.addEventListener('change', load);
            });
        }
        load();
    }

    function start() {
        document.querySelectorAll('[data-password-feedback]').forEach(setUp);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
