// Sign-in screens: choosing another provider on the single sign-on page shows its setup steps,
// and a Download button saves the text of the element it names (new recovery codes).
document.querySelectorAll('[data-preset-select]').forEach((select) => {
    select.addEventListener('change', () => {
        const url = new URL(window.location.href);
        url.searchParams.set('preset', select.value);
        window.location.assign(url.toString());
    });
});

document.querySelectorAll('[data-download]').forEach((button) => {
    button.addEventListener('click', () => {
        const source = document.querySelector(button.dataset.download);
        if (!source) {
            return;
        }
        const blob = new Blob([source.textContent.trim() + '\n'], { type: 'text/plain' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = button.dataset.filename || 'download.txt';
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(link.href);
    });
});
