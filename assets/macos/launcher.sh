#!/bin/bash
# Spark macOS launcher — wraps the PyApp binary.
# PyApp now has a built-in native splash screen for first-run bootstrap.
# This launcher handles upgrade detection (clears PyApp cache when binary changes).

set -e

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${SELF_DIR}/spark-engine"

# PyApp stores its data under ~/Library/Application Support/pyapp.
PYAPP_DATA="${HOME}/Library/Application Support/pyapp"

# Track the engine binary's checksum to detect upgrades.
# If the binary changed since last launch, clear the PyApp cache
# so the new embedded wheel is extracted fresh.
VERSION_MARKER="${PYAPP_DATA}/.spark-engine-checksum"
CURRENT_CHECKSUM=$(md5 -q "${ENGINE}" 2>/dev/null || echo "unknown")

if [ -f "${VERSION_MARKER}" ]; then
    PREV_CHECKSUM=$(cat "${VERSION_MARKER}" 2>/dev/null || echo "")
    if [ "${CURRENT_CHECKSUM}" != "${PREV_CHECKSUM}" ]; then
        # Upgrade detected — clear PyApp cache
        rm -rf "${PYAPP_DATA}"
        mkdir -p "${PYAPP_DATA}"
    fi
fi

# Store checksum for future upgrade detection
mkdir -p "${PYAPP_DATA}"
echo "${CURRENT_CHECKSUM}" > "${VERSION_MARKER}"

# Launch the engine — PyApp handles splash screen natively
exec "${ENGINE}" "$@"
