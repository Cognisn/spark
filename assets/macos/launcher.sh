#!/bin/bash
# Spark macOS launcher — wraps the PyApp binary to show a splash on first run.
# On first launch, PyApp extracts the embedded Python and installs dependencies.
# This script shows a native macOS dialog so the user knows what's happening.

set -e

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${SELF_DIR}/spark-engine"

# PyApp stores its data under ~/.local/share/pyapp or ~/Library/Application Support/pyapp.
# The project-specific directory is based on a hash. We check whether the engine has
# been run before by looking for any pyapp data directory with content.
PYAPP_DATA="${HOME}/Library/Application Support/pyapp"
FIRST_RUN=false

if [ ! -d "${PYAPP_DATA}" ] || [ -z "$(ls -A "${PYAPP_DATA}" 2>/dev/null)" ]; then
    FIRST_RUN=true
fi

if [ "${FIRST_RUN}" = true ]; then
    # Show a native macOS splash dialog (non-blocking, auto-dismisses when engine starts)
    osascript -e '
        tell application "System Events"
            activate
            display dialog "Spark is setting up its environment for the first time." & return & return & "This includes extracting the Python runtime and installing dependencies. It may take a minute or two." & return & return & "The application will open in your browser when ready. This dialog will close automatically." buttons {"OK"} with title "Spark — First Launch" with icon note giving up after 120
        end tell
    ' &
    SPLASH_PID=$!

    # Launch the engine — once it starts, the web UI loading page takes over
    "${ENGINE}" "$@" &
    ENGINE_PID=$!

    # Wait a bit then kill the splash dialog if still open
    (
        sleep 30
        kill "${SPLASH_PID}" 2>/dev/null || true
    ) &

    wait "${ENGINE_PID}" 2>/dev/null
else
    # Subsequent launches — run directly
    exec "${ENGINE}" "$@"
fi
