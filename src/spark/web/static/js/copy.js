// Copy buttons: <button data-copy="#element-id"> copies that element's text.
document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', () => {
        const source = document.querySelector(button.dataset.copy);
        if (source && navigator.clipboard) {
            navigator.clipboard.writeText(source.textContent.trim());
            button.innerHTML = '<i class="bi bi-check2"></i> Copied';
        }
    });
});
