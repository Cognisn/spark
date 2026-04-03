#!/bin/bash
# Spark macOS launcher — wraps the PyApp binary to show a splash on first run.
# On first launch, PyApp extracts the embedded Python and installs dependencies.
# This script shows a native macOS dialog so the user knows what's happening.
# On upgrade (new binary version), it clears the PyApp cache so the new wheel is used.

set -e

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${SELF_DIR}/spark-engine"

# PyApp stores its data under ~/Library/Application Support/pyapp.
PYAPP_DATA="${HOME}/Library/Application Support/pyapp"

# Track the engine binary's checksum to detect upgrades.
# If the binary changed since last launch, clear the PyApp cache.
VERSION_MARKER="${PYAPP_DATA}/.spark-engine-checksum"
CURRENT_CHECKSUM=$(md5 -q "${ENGINE}" 2>/dev/null || echo "unknown")

FIRST_RUN=false
UPGRADE=false

if [ ! -d "${PYAPP_DATA}" ] || [ -z "$(ls -A "${PYAPP_DATA}" 2>/dev/null)" ]; then
    FIRST_RUN=true
elif [ -f "${VERSION_MARKER}" ]; then
    PREV_CHECKSUM=$(cat "${VERSION_MARKER}" 2>/dev/null || echo "")
    if [ "${CURRENT_CHECKSUM}" != "${PREV_CHECKSUM}" ]; then
        UPGRADE=true
    fi
else
    # Marker doesn't exist but pyapp data does — first run with this launcher version
    # Write the marker for next time
    mkdir -p "${PYAPP_DATA}"
    echo "${CURRENT_CHECKSUM}" > "${VERSION_MARKER}"
fi

if [ "${UPGRADE}" = true ]; then
    # Clear PyApp cache so the new embedded wheel is extracted
    rm -rf "${PYAPP_DATA}"
    mkdir -p "${PYAPP_DATA}"
    echo "${CURRENT_CHECKSUM}" > "${VERSION_MARKER}"
    FIRST_RUN=true

    osascript -e '
        tell application "System Events"
            activate
            display dialog "Spark is upgrading to a new version." & return & return & "This may take a minute. The application will open in your browser when ready." buttons {"OK"} with title "Spark — Upgrading" with icon note giving up after 120
        end tell
    ' &
    SPLASH_PID=$!

    "${ENGINE}" "$@" &
    ENGINE_PID=$!

    ( sleep 30; kill "${SPLASH_PID}" 2>/dev/null || true ) &
    wait "${ENGINE_PID}" 2>/dev/null
elif [ "${FIRST_RUN}" = true ]; then
    # Show first-run splash
    osascript -e '
        tell application "System Events"
            activate
            display dialog "Spark is setting up its environment for the first time." & return & return & "This includes extracting the Python runtime and installing dependencies. It may take a minute or two." & return & return & "The application will open in your browser when ready. This dialog will close automatically." buttons {"OK"} with title "Spark — First Launch" with icon note giving up after 120
        end tell
    ' &
    SPLASH_PID=$!

    "${ENGINE}" "$@" &
    ENGINE_PID=$!

    ( sleep 30; kill "${SPLASH_PID}" 2>/dev/null || true ) &
    wait "${ENGINE_PID}" 2>/dev/null

    # Store checksum for future upgrade detection
    mkdir -p "${PYAPP_DATA}"
    echo "${CURRENT_CHECKSUM}" > "${VERSION_MARKER}"
else
    # Subsequent launches — run directly
    exec "${ENGINE}" "$@"
fi
