/**
 * Pages before sign-in: a Show toggle on every password field.
 *
 * The field works without this script. The toggle is a real button with an accessible name,
 * 44 px tall, and it leaves the field's value, autocomplete and focus alone.
 */
(function () {
    'use strict';

    document.querySelectorAll('.auth-card input[type="password"]').forEach(function (input) {
        if (input.closest('.auth-password')) return;

        var wrap = document.createElement('div');
        wrap.className = 'auth-password';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'auth-password-toggle';
        button.textContent = 'Show';
        button.setAttribute('aria-pressed', 'false');
        button.setAttribute('aria-label', 'Show password');
        if (input.id) button.setAttribute('aria-controls', input.id);
        wrap.appendChild(button);

        button.addEventListener('click', function () {
            var show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            button.textContent = show ? 'Hide' : 'Show';
            button.setAttribute('aria-pressed', show ? 'true' : 'false');
            button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
        });
    });
}());
