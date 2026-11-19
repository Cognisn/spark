#!/usr/bin/env bash
# Check a built Spark image: it runs as a non-root user, carries no compiler or
# installer, has a healthcheck on /health, carries the RDS certificate authority
# bundle at its fixed path, holds no tests, its worker role refuses personal mode,
# and no NVIDIA package is installed. Exits non-zero on the first failure.
#
#   deploy/check-image.sh spark:dev
set -euo pipefail

image="${1:?usage: deploy/check-image.sh IMAGE}"

fail() {
    echo "check-image: FAIL: $*" >&2
    exit 1
}

pass() {
    echo "check-image: ok: $*"
}

# 1. The configured user is not root, by name or by id.
user="$(docker image inspect --format '{{.Config.User}}' "$image")"
case "${user%%:*}" in
    "" | root | 0) fail "the image is configured to run as root (USER '${user}')" ;;
esac
uid="$(docker run --rm --entrypoint id "$image" -u)"
[ "$uid" != "0" ] || fail "the container runs as uid 0"
pass "runs as uid ${uid} (USER ${user})"

# 2. No compiler, make or pip in the final stage.
if ! docker run --rm --entrypoint sh "$image" -c '
    for tool in gcc cc c++ g++ make pip pip3; do
        if command -v "$tool" >/dev/null 2>&1; then echo "found $tool"; exit 1; fi
    done
    if python -c "import pip" >/dev/null 2>&1; then echo "found the pip module"; exit 1; fi
'; then
    fail "build tools or an installer are present"
fi
pass "no compiler, make or pip"

# 3. A healthcheck is defined (for the web role).
healthcheck="$(docker image inspect --format '{{if .Config.Healthcheck}}{{json .Config.Healthcheck.Test}}{{end}}' "$image")"
[ -n "$healthcheck" ] || fail "no healthcheck"
case "$healthcheck" in
    */health*) ;;
    *) fail "the healthcheck does not probe /health: ${healthcheck}" ;;
esac
pass "healthcheck ${healthcheck}"

# 3b. The RDS certificate authority bundle, at the path tasks name in PGSSLROOTCERT,
# readable by the unprivileged user, public certificates only.
rds_bundle=/etc/ssl/certs/rds-global-bundle.pem
if ! docker run --rm --entrypoint sh "$image" -c '
    f=/etc/ssl/certs/rds-global-bundle.pem
    [ -f "$f" ] || { echo "missing"; exit 1; }
    [ -r "$f" ] || { echo "not readable by uid $(id -u)"; exit 1; }
    grep -q "BEGIN CERTIFICATE" "$f" || { echo "no certificates"; exit 1; }
    if grep -q "PRIVATE KEY" "$f"; then echo "holds a private key"; exit 1; fi
'; then
    fail "the RDS certificate bundle at ${rds_bundle} is missing, unreadable or not certificate-only"
fi
pass "RDS certificate bundle at ${rds_bundle}, readable by uid ${uid}"

# 4. No tests or build context in the image.
if ! docker run --rm --entrypoint python "$image" -c '
import importlib.util, os, sys
sys.exit(1 if os.path.exists("/build") or importlib.util.find_spec("tests") else 0)
'; then
    fail "tests or the build context are in the image"
fi
pass "no tests or build context"

# 5. The worker role refuses personal mode (the default) with exit status 2.
set +e
docker run --rm "$image" worker >/dev/null 2>&1
status=$?
set -e
[ "$status" -eq 2 ] || fail "spark worker in personal mode exited ${status}, expected 2"
pass "spark worker refuses personal mode"

# 6. No NVIDIA (CUDA) package: the image carries the CPU-only PyTorch wheel.
nvidia="$(docker run --rm --entrypoint python "$image" -c '
import re
from importlib import metadata
names = {re.sub(r"[-_.]+", "-", d.metadata["Name"] or "").lower() for d in metadata.distributions()}
print(" ".join(sorted(n for n in names if n.startswith("nvidia-"))))
')"
[ -z "$nvidia" ] || fail "NVIDIA packages are installed: ${nvidia}"
pass "no NVIDIA packages (CPU-only torch)"

echo "check-image: all checks passed for ${image}"
