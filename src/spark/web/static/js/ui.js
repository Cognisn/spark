/**
 * Unified UI Component Library
 * Theme switching, DataTable, Toast notifications, Chart.js helpers
 * Brand-agnostic — works with any theme.css
 */

/* ==========================================================================
   1. Theme Manager
   ========================================================================== */

const AppTheme = {
    STORAGE_KEY: 'app-theme',
    DARK: 'dark',
    LIGHT: 'light',

    init() {
        const saved = localStorage.getItem(this.STORAGE_KEY);
        const preferred = window.matchMedia('(prefers-color-scheme: light)').matches ? this.LIGHT : this.DARK;
        this.set(saved || preferred);

        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
            if (!localStorage.getItem(this.STORAGE_KEY)) {
                this.set(e.matches ? this.DARK : this.LIGHT);
            }
        });
    },

    get() {
        return document.documentElement.getAttribute('data-bs-theme') || this.DARK;
    },

    set(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
        localStorage.setItem(this.STORAGE_KEY, theme);
        this._updateToggles(theme);
        this._updateCharts(theme);
        document.dispatchEvent(new CustomEvent('app-theme-changed', { detail: { theme } }));
    },

    toggle() {
        this.set(this.get() === this.DARK ? this.LIGHT : this.DARK);
    },

    _updateToggles(theme) {
        document.querySelectorAll('.app-theme-toggle .theme-option').forEach(el => {
            el.classList.toggle('active', el.dataset.theme === theme);
        });
    },

    _updateCharts(theme) {
        if (typeof Chart !== 'undefined' && Chart.instances) {
            Object.values(Chart.instances).forEach(chart => {
                AppChart.applyTheme(chart);
                chart.update('none');
            });
        }
    }
};


/* ==========================================================================
   2. DataTable — Sortable, Filterable, Paginated
   ========================================================================== */

class AppDataTable {
    /**
     * @param {HTMLElement|string} wrapper - The .app-table-wrapper element or selector
     * @param {Object} options
     * @param {Array<Object>} options.data - Array of row objects
     * @param {Array<Object>} options.columns - [{key, label, sortable, filterable, render, width, align}]
     * @param {number} [options.pageSize=10]
     * @param {number[]} [options.pageSizes=[10,25,50,100]]
     * @param {boolean} [options.striped=false]
     * @param {Function} [options.onRowClick]
     */
    constructor(wrapper, options) {
        this.wrapper = typeof wrapper === 'string' ? document.querySelector(wrapper) : wrapper;
        this.options = Object.assign({
            data: [],
            columns: [],
            pageSize: 10,
            pageSizes: [10, 25, 50, 100],
            striped: false,
            onRowClick: null
        }, options);

        this.allData = [...this.options.data];
        this.filteredData = [...this.allData];
        this.sortColumn = null;
        this.sortDirection = null;
        this.currentPage = 1;
        this.searchQuery = '';
        this.columnFilters = {};

        this._render();
        this._bindEvents();
    }

    _render() {
        this.wrapper.innerHTML = '';
        this.wrapper.classList.add('app-table-wrapper');

        const toolbar = document.createElement('div');
        toolbar.className = 'app-table-toolbar';
        toolbar.innerHTML = `
            <div class="toolbar-left">
                <div class="app-table-search">
                    <i class="bi bi-search search-icon"></i>
                    <input type="text" class="form-control form-control-sm" placeholder="Search..." data-action="search">
                </div>
            </div>
            <div class="toolbar-right">
                <select class="form-select form-select-sm" data-action="page-size" style="width: auto;">
                    ${this.options.pageSizes.map(s =>
                        `<option value="${s}" ${s === this.options.pageSize ? 'selected' : ''}>${s} per page</option>`
                    ).join('')}
                </select>
            </div>
        `;
        this.wrapper.appendChild(toolbar);

        const tableContainer = document.createElement('div');
        tableContainer.style.overflowX = 'auto';
        const table = document.createElement('table');
        table.className = `app-table${this.options.striped ? ' app-table-striped' : ''}`;

        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        this.options.columns.forEach(col => {
            const th = document.createElement('th');
            th.textContent = col.label || col.key;
            if (col.sortable !== false) {
                th.classList.add('sortable');
                th.dataset.column = col.key;
            }
            if (col.width) th.style.width = col.width;
            if (col.align) th.style.textAlign = col.align;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        this.tbody = document.createElement('tbody');
        table.appendChild(this.tbody);
        tableContainer.appendChild(table);
        this.wrapper.appendChild(tableContainer);

        this.footer = document.createElement('div');
        this.footer.className = 'app-table-footer';
        this.wrapper.appendChild(this.footer);

        this._updateTable();
    }

    _bindEvents() {
        const searchInput = this.wrapper.querySelector('[data-action="search"]');
        let debounce = null;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                this.searchQuery = e.target.value.trim().toLowerCase();
                this.currentPage = 1;
                this._applyFilters();
            }, 250);
        });

        this.wrapper.querySelector('[data-action="page-size"]').addEventListener('change', (e) => {
            this.options.pageSize = parseInt(e.target.value);
            this.currentPage = 1;
            this._updateTable();
        });

        this.wrapper.addEventListener('click', (e) => {
            const th = e.target.closest('th.sortable');
            if (th) {
                const col = th.dataset.column;
                if (this.sortColumn === col) {
                    this.sortDirection = this.sortDirection === 'asc' ? 'desc' : this.sortDirection === 'desc' ? null : 'asc';
                    if (!this.sortDirection) this.sortColumn = null;
                } else {
                    this.sortColumn = col;
                    this.sortDirection = 'asc';
                }
                this._applySort();
            }

            const pageBtn = e.target.closest('.page-btn');
            if (pageBtn && !pageBtn.disabled) {
                const page = pageBtn.dataset.page;
                if (page === 'prev') this.currentPage--;
                else if (page === 'next') this.currentPage++;
                else this.currentPage = parseInt(page);
                this._updateTable();
            }
        });

        if (this.options.onRowClick) {
            this.tbody.addEventListener('click', (e) => {
                const tr = e.target.closest('tr');
                if (tr && tr.dataset.index !== undefined) {
                    this.options.onRowClick(this.filteredData[parseInt(tr.dataset.index)]);
                }
            });
        }
    }

    _applyFilters() {
        this.filteredData = this.allData.filter(row => {
            if (this.searchQuery) {
                const values = this.options.columns.map(col => {
                    const val = row[col.key];
                    return val != null ? String(val).toLowerCase() : '';
                });
                if (!values.some(v => v.includes(this.searchQuery))) return false;
            }
            for (const [key, filterVal] of Object.entries(this.columnFilters)) {
                if (filterVal && String(row[key]).toLowerCase() !== filterVal.toLowerCase()) return false;
            }
            return true;
        });
        this._applySort();
    }

    _applySort() {
        if (this.sortColumn && this.sortDirection) {
            const col = this.sortColumn;
            const dir = this.sortDirection === 'asc' ? 1 : -1;
            this.filteredData.sort((a, b) => {
                let va = a[col], vb = b[col];
                if (va == null) va = '';
                if (vb == null) vb = '';
                if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
                return String(va).localeCompare(String(vb)) * dir;
            });
        }
        this._updateTable();
    }

    _updateTable() {
        const total = this.filteredData.length;
        const pageSize = this.options.pageSize;
        const totalPages = Math.max(1, Math.ceil(total / pageSize));
        this.currentPage = Math.min(this.currentPage, totalPages);
        const start = (this.currentPage - 1) * pageSize;
        const pageData = this.filteredData.slice(start, start + pageSize);

        this.wrapper.querySelectorAll('th.sortable').forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (th.dataset.column === this.sortColumn) {
                if (this.sortDirection === 'asc') th.classList.add('sort-asc');
                else if (this.sortDirection === 'desc') th.classList.add('sort-desc');
            }
        });

        this.tbody.innerHTML = '';
        if (pageData.length === 0) {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td colspan="${this.options.columns.length}" style="text-align:center; padding:2rem; color:var(--app-text-muted);">No results found</td>`;
            this.tbody.appendChild(tr);
        } else {
            pageData.forEach((row, i) => {
                const tr = document.createElement('tr');
                tr.dataset.index = start + i;
                if (this.options.onRowClick) tr.style.cursor = 'pointer';
                this.options.columns.forEach(col => {
                    const td = document.createElement('td');
                    if (col.align) td.style.textAlign = col.align;
                    if (col.render) {
                        td.innerHTML = col.render(row[col.key], row);
                    } else {
                        td.textContent = row[col.key] != null ? row[col.key] : '';
                    }
                    tr.appendChild(td);
                });
                this.tbody.appendChild(tr);
            });
        }

        const showing = total > 0 ? `Showing ${start + 1}\u2013${Math.min(start + pageSize, total)} of ${total}` : 'No results';
        let paginationHTML = '';
        if (totalPages > 1) {
            paginationHTML = `<div class="app-pagination">`;
            paginationHTML += `<button class="page-btn" data-page="prev" ${this.currentPage <= 1 ? 'disabled' : ''}>&laquo;</button>`;
            const range = this._pageRange(this.currentPage, totalPages);
            range.forEach(p => {
                if (p === '...') {
                    paginationHTML += `<span style="padding: 0 0.25rem; color: var(--app-text-muted);">\u2026</span>`;
                } else {
                    paginationHTML += `<button class="page-btn ${p === this.currentPage ? 'active' : ''}" data-page="${p}">${p}</button>`;
                }
            });
            paginationHTML += `<button class="page-btn" data-page="next" ${this.currentPage >= totalPages ? 'disabled' : ''}>&raquo;</button>`;
            paginationHTML += `</div>`;
        }
        this.footer.innerHTML = `<span>${showing}</span>${paginationHTML}`;
    }

    _pageRange(current, total) {
        if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
        const pages = [];
        pages.push(1);
        if (current > 3) pages.push('...');
        for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) {
            pages.push(i);
        }
        if (current < total - 2) pages.push('...');
        pages.push(total);
        return pages;
    }

    setData(data) { this.allData = [...data]; this.currentPage = 1; this._applyFilters(); }
    addRows(rows) { this.allData.push(...rows); this._applyFilters(); }
    setColumnFilter(key, value) {
        if (value) this.columnFilters[key] = value;
        else delete this.columnFilters[key];
        this.currentPage = 1;
        this._applyFilters();
    }
    refresh() { this._updateTable(); }
}


/* ==========================================================================
   3. Toast Notifications
   ========================================================================== */

const AppToast = {
    _container: null,

    _getContainer() {
        if (!this._container) {
            this._container = document.createElement('div');
            this._container.className = 'app-toast-container';
            document.body.appendChild(this._container);
        }
        return this._container;
    },

    show({ title, message, type = 'info', duration = 5000, icon } = {}) {
        const icons = {
            success: 'bi-check-circle-fill',
            warning: 'bi-exclamation-triangle-fill',
            danger: 'bi-x-circle-fill',
            info: 'bi-info-circle-fill'
        };

        const toast = document.createElement('div');
        toast.className = `app-toast toast-${type}`;
        toast.innerHTML = `
            <i class="bi ${icon || icons[type] || icons.info} toast-icon"></i>
            <div class="toast-body">
                <div class="toast-title">${title || ''}</div>
                ${message ? `<div class="toast-message">${message}</div>` : ''}
            </div>
            <button class="toast-close" aria-label="Close">&times;</button>
        `;

        const container = this._getContainer();
        container.appendChild(toast);
        toast.querySelector('.toast-close').addEventListener('click', () => this._dismiss(toast));
        if (duration > 0) setTimeout(() => this._dismiss(toast), duration);
        return toast;
    },

    _dismiss(toast) {
        toast.style.animation = 'app-toast-out 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    },

    success(title, message) { return this.show({ title, message, type: 'success' }); },
    warning(title, message) { return this.show({ title, message, type: 'warning' }); },
    danger(title, message) { return this.show({ title, message, type: 'danger' }); },
    info(title, message) { return this.show({ title, message, type: 'info' }); },
};

const toastStyle = document.createElement('style');
toastStyle.textContent = `
    @keyframes app-toast-out {
        from { transform: translateX(0); opacity: 1; }
        to { transform: translateX(120%); opacity: 0; }
    }
`;
document.head.appendChild(toastStyle);


/* ==========================================================================
   4. Chart.js Helpers
   ========================================================================== */

const AppChart = {
    getCSSVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    },

    getColours(count = 6) {
        const vars = [];
        for (let i = 1; i <= Math.min(count, 6); i++) {
            vars.push(this.getCSSVar(`--app-chart-${i}`));
        }
        while (vars.length < count) {
            vars.push(vars[vars.length % 6]);
        }
        return vars;
    },

    getDefaults() {
        const fontFamily = this.getCSSVar('--app-font-body') || "'Inter', sans-serif";
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false,
                    labels: {
                        color: this.getCSSVar('--app-chart-text'),
                        font: { family: fontFamily, size: 12 }
                    }
                },
                tooltip: {
                    backgroundColor: this.getCSSVar('--app-bg-primary'),
                    titleColor: this.getCSSVar('--app-text-primary'),
                    bodyColor: this.getCSSVar('--app-text-secondary'),
                    borderColor: this.getCSSVar('--app-card-border'),
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 10,
                    titleFont: { family: fontFamily, weight: '600', size: 13 },
                    bodyFont: { family: fontFamily, size: 12 },
                    displayColors: true,
                    boxPadding: 4,
                }
            },
            scales: {
                x: {
                    ticks: { color: this.getCSSVar('--app-chart-text'), font: { size: 11 } },
                    grid: { color: this.getCSSVar('--app-chart-grid'), drawBorder: false },
                    border: { display: false }
                },
                y: {
                    ticks: { color: this.getCSSVar('--app-chart-text'), font: { size: 11 } },
                    grid: { color: this.getCSSVar('--app-chart-grid'), drawBorder: false },
                    border: { display: false }
                }
            }
        };
    },

    applyTheme(chart) {
        const defaults = this.getDefaults();
        if (chart.options.plugins) {
            if (chart.options.plugins.legend) {
                chart.options.plugins.legend.labels = {
                    ...chart.options.plugins.legend.labels,
                    color: this.getCSSVar('--app-chart-text')
                };
            }
            if (chart.options.plugins.tooltip) {
                Object.assign(chart.options.plugins.tooltip, defaults.plugins.tooltip);
            }
        }
        if (chart.options.scales) {
            ['x', 'y'].forEach(axis => {
                if (chart.options.scales[axis]) {
                    if (chart.options.scales[axis].ticks) {
                        chart.options.scales[axis].ticks.color = this.getCSSVar('--app-chart-text');
                    }
                    if (chart.options.scales[axis].grid) {
                        chart.options.scales[axis].grid.color = this.getCSSVar('--app-chart-grid');
                    }
                }
            });
        }
    },

    line(canvas, config) {
        const ctx = (typeof canvas === 'string' ? document.querySelector(canvas) : canvas).getContext('2d');
        const colours = this.getColours(config.datasets.length);
        const defaults = this.getDefaults();
        config.datasets.forEach((ds, i) => {
            ds.borderColor = ds.borderColor || colours[i];
            ds.backgroundColor = ds.backgroundColor || colours[i] + '20';
            ds.borderWidth = ds.borderWidth || 2;
            ds.tension = ds.tension ?? 0.4;
            ds.fill = ds.fill ?? true;
            ds.pointRadius = ds.pointRadius ?? 3;
            ds.pointHoverRadius = ds.pointHoverRadius ?? 5;
            ds.pointBackgroundColor = ds.pointBackgroundColor || colours[i];
        });
        return new Chart(ctx, { type: 'line', data: { labels: config.labels, datasets: config.datasets }, options: defaults });
    },

    bar(canvas, config) {
        const ctx = (typeof canvas === 'string' ? document.querySelector(canvas) : canvas).getContext('2d');
        const colours = this.getColours(config.datasets.length);
        const defaults = this.getDefaults();
        config.datasets.forEach((ds, i) => {
            ds.backgroundColor = ds.backgroundColor || colours[i] + 'CC';
            ds.borderColor = ds.borderColor || colours[i];
            ds.borderWidth = ds.borderWidth || 1;
            ds.borderRadius = ds.borderRadius ?? 4;
            ds.hoverBackgroundColor = ds.hoverBackgroundColor || colours[i];
        });
        return new Chart(ctx, { type: 'bar', data: { labels: config.labels, datasets: config.datasets }, options: defaults });
    },

    doughnut(canvas, config) {
        const ctx = (typeof canvas === 'string' ? document.querySelector(canvas) : canvas).getContext('2d');
        const colours = this.getColours(config.data.length);
        return new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: config.labels,
                datasets: [{
                    data: config.data,
                    backgroundColor: config.colours || colours.map(c => c + 'CC'),
                    borderColor: config.colours || colours,
                    borderWidth: 2,
                    hoverOffset: 8
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '65%',
                plugins: {
                    legend: { display: false },
                    tooltip: this.getDefaults().plugins.tooltip
                }
            }
        });
    }
};


/* ==========================================================================
   5. Sidebar Toggle (Mobile)
   ========================================================================== */

const AppSidebar = {
    init() {
        const toggleBtn = document.querySelector('[data-action="toggle-sidebar"]');
        const sidebar = document.querySelector('.app-sidebar');
        const overlay = document.querySelector('.app-sidebar-overlay');

        if (!toggleBtn || !sidebar) return;

        toggleBtn.addEventListener('click', () => {
            sidebar.classList.toggle('show');
            overlay?.classList.toggle('show');
        });

        overlay?.addEventListener('click', () => {
            sidebar.classList.remove('show');
            overlay.classList.remove('show');
        });
    }
};


/* ==========================================================================
   6. Initialisation
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
    AppTheme.init();
    AppSidebar.init();

    document.querySelectorAll('.app-theme-toggle .theme-option').forEach(el => {
        el.addEventListener('click', () => AppTheme.set(el.dataset.theme));
    });
});
