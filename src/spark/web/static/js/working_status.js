/**
 * Spark: the working indicator and status line.
 *
 * While a turn runs, a person sees that the AI is working and what it is doing, so a
 * long turn never looks stalled. The indicator ("Thinking...") shows from the moment a
 * message is sent; under it a one-line status follows the events the stream already
 * sends ("Searching the web", "Reading report.pdf", "Compacting the conversation").
 * After a few seconds on one step the time spent on it is added.
 *
 * Only a tool's name, and a file's name for the tools that read one, is ever shown:
 * never a tool's input or result, and never anything as HTML (textContent only).
 *
 * The block is a polite live region: a screen reader hears a change of step, not the
 * clock, which sits in an aria-hidden element. Under prefers-reduced-motion the dots
 * do not move (chat_status.css).
 */
(function () {
    const SHOW_ELAPSED_AFTER_MS = 5000;
    const TICK_MS = 1000;
    const NAME_LIMIT = 40;

    // Friendly names for the built-in tools, in one table. A tool that is not here (an
    // MCP tool, or one added later) is shown by its own name.
    const TOOL_LABELS = {
        web_search: 'Searching the web',
        web_fetch: 'Reading a web page',
        read_file: 'Reading a file',
        read_pdf: 'Reading a PDF',
        read_word: 'Reading a Word document',
        read_excel: 'Reading a spreadsheet',
        read_powerpoint: 'Reading a presentation',
        read_my_file: 'Reading a file',
        list_my_files: 'Looking through your files',
        list_directory: 'Looking through a folder',
        get_directory_tree: 'Looking through a folder',
        get_file_info: 'Checking a file',
        search_files: 'Searching files',
        find_in_file: 'Searching a file',
        write_file: 'Writing a file',
        create_pdf: 'Creating a PDF',
        create_word: 'Creating a Word document',
        create_excel: 'Creating a spreadsheet',
        create_powerpoint: 'Creating a presentation',
        create_chart: 'Creating a chart',
        create_diagram: 'Creating a diagram',
        show_document: 'Opening a document',
        run_command: 'Running a command',
        draft_email: 'Drafting an email',
        send_email: 'Sending an email',
        store_memory: 'Saving a memory',
        query_memory: 'Checking memory',
        list_memories: 'Checking memory',
        delete_memory: 'Removing a memory',
        get_current_datetime: 'Checking the time',
        get_tool_documentation: 'Reading tool documentation',
        extract_archive: 'Unpacking an archive',
        list_archive: 'Looking inside an archive',
        create_project: 'Creating a project',
        suggest_project: 'Suggesting a project',
        update_project: 'Updating a project',
        move_conversation_to_project: 'Moving this conversation to a project',
    };

    // The tools whose file argument is named in the status ("Reading report.pdf").
    const READ_TOOLS = new Set(['read_file', 'read_pdf', 'read_word', 'read_excel', 'read_powerpoint']);

    function fileName(params) {
        const path = params && typeof params.path === 'string' ? params.path : '';
        const base = path.split(/[\\/]/).filter(Boolean).pop() || '';
        // Printable characters only, and short: a status line, not a place for content
        const clean = base.replace(/[\u0000-\u001f\u007f]/g, '').trim();
        if (!clean) return '';
        return clean.length > NAME_LIMIT ? clean.slice(0, NAME_LIMIT - 1) + '…' : clean;
    }

    /** What to say while a tool runs. Takes the tool's name and its input; shows neither's content. */
    function toolLabel(name, params) {
        const tool = typeof name === 'string' ? name : '';
        if (Object.prototype.hasOwnProperty.call(TOOL_LABELS, tool)) {
            if (READ_TOOLS.has(tool)) {
                const file = fileName(params);
                if (file) return `Reading ${file}`;
            }
            return TOOL_LABELS[tool];
        }
        const shown = tool.length > 60 ? tool.slice(0, 59) + '…' : tool;
        return shown ? `Running a tool: ${shown}` : 'Running a tool';
    }

    /** "12s", "1m 05s". */
    function elapsedText(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        if (total < 60) return `${total}s`;
        return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
    }

    /**
     * One working indicator. `host` is where it is placed; `keepLast` keeps it the last
     * child of a list that grows (the chat). `idle` is the first line ("Thinking...").
     */
    function create(host, options) {
        const opts = options || {};
        const idle = opts.idle || 'Thinking...';
        const clock = opts.clock || (() => Date.now());

        let root = null;
        let indicatorLine = null;
        let detailLine = null;
        let detailText = null;
        let detailTime = null;
        let timer = null;
        let stepStart = 0;
        let hiddenPhase = false;       // the reasoning box or the reply stands in for it
        let compacting = false;
        const running = new Map();     // id -> label, in the order started
        let lastDetail = null;

        function build() {
            root = document.createElement('div');
            root.className = 'working-status';
            root.setAttribute('role', 'status');
            root.setAttribute('aria-live', 'polite');
            if (opts.className) root.classList.add(opts.className);

            indicatorLine = document.createElement('div');
            indicatorLine.className = 'working-indicator';
            const dots = document.createElement('span');
            dots.className = 'working-dots';
            dots.setAttribute('aria-hidden', 'true');
            for (let i = 0; i < 3; i++) dots.appendChild(document.createElement('span'));
            const label = document.createElement('span');
            label.className = 'working-label';
            label.textContent = idle;
            indicatorLine.appendChild(dots);
            indicatorLine.appendChild(label);

            detailLine = document.createElement('div');
            detailLine.className = 'working-detail';
            detailText = document.createElement('span');
            detailText.className = 'working-step';
            // The clock is decoration for the eye: a screen reader hears the step, not the seconds
            detailTime = document.createElement('span');
            detailTime.className = 'working-time';
            detailTime.setAttribute('aria-hidden', 'true');
            detailLine.appendChild(detailText);
            detailLine.appendChild(detailTime);

            root.appendChild(indicatorLine);
            root.appendChild(detailLine);
        }

        function currentDetail() {
            if (compacting) return 'Compacting the conversation';
            if (running.size) return Array.from(running.values()).pop();
            return '';
        }

        function renderTime() {
            if (!root) return;
            const ms = clock() - stepStart;
            detailTime.textContent = detailText.textContent && ms >= SHOW_ELAPSED_AFTER_MS
                ? ` (${elapsedText(ms)})` : '';
        }

        function render() {
            if (!root) return;
            root.hidden = hiddenPhase;
            const detail = currentDetail();
            if (detail !== lastDetail) {
                lastDetail = detail;
                stepStart = clock();
                detailText.textContent = detail;
            }
            detailLine.hidden = !detail;
            renderTime();
            keepLast();
        }

        function keepLast() {
            if (root && opts.keepLast && host.lastElementChild !== root) host.appendChild(root);
        }

        function ensure() {
            if (root) return;
            build();
            stepStart = clock();
            if (opts.after) opts.after.insertAdjacentElement('afterend', root);
            else host.appendChild(root);
            timer = setInterval(renderTime, TICK_MS);
        }

        const api = {
            /** The turn started: "Thinking..." shows at once. */
            begin() {
                ensure();
                hiddenPhase = false;
                render();
            },
            /** A tool (or any step) started; `id` pairs it with its end. */
            start(id, label) {
                ensure();
                running.set(id, label);
                hiddenPhase = false;
                render();
            },
            /** The tool (or step) with this id ended. */
            finish(id) {
                if (!root) return;
                running.delete(id);
                render();
            },
            /** A tool started, by name and input. */
            tool(id, name, params) {
                api.start(`tool:${id}`, toolLabel(name, params));
            },
            toolDone(id) {
                api.finish(`tool:${id}`);
            },
            agent(id) {
                api.start(`agent:${id}`, 'Working with an agent');
            },
            agentDone(id) {
                api.finish(`agent:${id}`);
            },
            compaction(on) {
                if (on) ensure();
                if (!root) return;
                compacting = !!on;
                hiddenPhase = false;
                render();
            },
            /** The reasoning box or the reply is showing: the indicator steps aside. */
            stepAside() {
                if (!root) return;
                hiddenPhase = true;
                render();
            },
            keepLast,
            /** The turn is over (ended, stopped or failed): everything goes. */
            end() {
                if (timer) clearInterval(timer);
                timer = null;
                if (root && root.parentNode) root.parentNode.removeChild(root);
                root = indicatorLine = detailLine = detailText = detailTime = null;
                running.clear();
                compacting = false;
                hiddenPhase = false;
                lastDetail = null;
            },
            active() {
                return !!root;
            },
        };
        return api;
    }

    window.SparkWorking = { create, toolLabel, elapsedText, TOOL_LABELS, SHOW_ELAPSED_AFTER_MS };
})();
