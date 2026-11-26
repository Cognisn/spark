#!/usr/bin/env bash
# The wave gate: the full suite, run locally on the release branch tip at the end
# of each wave (the repository's "Full suite command"). It covers what the Linux
# jobs of .github/workflows/ci.yml cover, and nothing in it is skipped or
# deselected to make it pass:
#
#   1. black --check and isort --check-only (the versions pinned in the dev extra)
#   2. the blocking mypy --strict list (the advisory mypy run over src/spark is
#      left out, as it is in CI)
#   3. the licence audit
#   4. the SQLite suite:      pytest -m "not postgres and not integration"
#   5. the PostgreSQL suites: pytest -m "postgres and not integration"
#      (pgvector/pgvector:pg16 through testcontainers, SPARK_TEST_POSTGRES=required)
#   6. the uv smoke test: build the wheel, install it as a uv tool, migrate, launch
#      headless and poll until it serves (in a scratch home, never the user's data)
#   7. the container image build and deploy/check-image.sh
#
# Left out, because they need a live stack or provider, or another operating
# system: tests/live and every test marked "integration" (CI deselects them too),
# the macOS and Windows personal-mode runs, the Python 3.13 runs, and the
# PostgreSQL migrate-twice step run against a service container (the PostgreSQL
# suites exercise the same migration chain).
#
# Run it from the repository root with the project's virtual environment active
# (pip install -e ".[dev]"), Docker running, and uv on PATH:
#
#   bash scripts/wave-gate.sh
#
# Check the machine's heavy-workload cap before starting: the PostgreSQL suites
# count as two heavy workloads. Environment overrides:
#   WAVE_GATE_WORK      scratch folder (default: a new temporary folder, removed on exit)
#   WAVE_GATE_WEB_PORT  port for the uv smoke test (default 8765)
#   WAVE_GATE_TAG       local image tag for the container build (default spark:wave-gate)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
root="$(pwd)"

work="${WAVE_GATE_WORK:-$(mktemp -d)}"
mkdir -p "$work"
web_port="${WAVE_GATE_WEB_PORT:-8765}"
image="${WAVE_GATE_TAG:-spark:wave-gate}"
smoke_pid=""

cleanup() {
    if [ -n "$smoke_pid" ]; then
        kill "$smoke_pid" 2>/dev/null || true
    fi
    docker image rm "$image" >/dev/null 2>&1 || true
    if [ -z "${WAVE_GATE_WORK:-}" ]; then
        rm -rf "$work" 2>/dev/null || true
    fi
}
trap cleanup EXIT

summary=()
overall_start=$SECONDS

# step NAME COMMAND...: run a command, print its time, and fail the gate on error.
step() {
    local name="$1"
    shift
    local start=$SECONDS
    echo
    echo "=== wave-gate: ${name} ==="
    "$@"
    local took=$((SECONDS - start))
    summary+=("${name}: ok, ${took} s")
    echo "=== wave-gate: ${name}: ok (${took} s) ==="
}

# pytest_step NAME MARKER_EXPRESSION: run pytest, keep its log, print the final line.
pytest_step() {
    local name="$1" marker="$2"
    shift 2
    local log="$work/${name// /-}.log"
    local start=$SECONDS
    echo
    echo "=== wave-gate: ${name} ==="
    env "$@" python -m pytest -m "$marker" -p no:cacheprovider 2>&1 | tee "$log"
    local took=$((SECONDS - start))
    local counts
    counts="$(grep -E '^=+ .*(passed|failed|error|skipped).* in [0-9.]+s' "$log" | tail -n 1 || true)"
    summary+=("${name}: ok, ${took} s, ${counts}")
    echo "=== wave-gate: ${name}: ok (${took} s) ==="
}

mypy_strict() {
    python -m mypy --strict --platform linux \
        src/spark/db src/spark/database src/spark/migrations src/spark/mode.py \
        scripts/licence_audit.py src/spark/worker.py src/spark/launch.py \
        src/spark/daemon/guard.py deploy/image_tags.py \
        src/spark/index src/spark/knowledge/store.py \
        src/spark/instance_settings src/spark/files \
        src/spark/web/security.py src/spark/web/auth.py src/spark/web/session.py \
        src/spark/web/endpoints/auth.py src/spark/web/endpoints/files.py \
        src/spark/web/endpoints/settings.py \
        src/spark/tools/registry.py src/spark/tools/command_policy.py \
        src/spark/tools/documents.py src/spark/tools/archives.py \
        src/spark/tools/document_creation.py \
        src/spark/safety/untrusted.py src/spark/connections \
        src/spark/tools/filesystem.py src/spark/core/processes.py \
        src/spark/web/db.py scripts/check_no_nvidia.py \
        src/spark/identity src/spark/web/endpoints/identity.py \
        src/spark/web/scope.py src/spark/scheduler/owners.py \
        src/spark/web/interactions.py src/spark/web/models.py \
        src/spark/core/context_compaction.py \
        src/spark/web/qr.py src/spark/web/endpoints/lifecycle.py \
        src/spark/web/endpoints/account.py src/spark/web/menu.py \
        src/spark/web/endpoints/administration.py src/spark/web/endpoints/audit.py \
        src/spark/usage src/spark/web/endpoints/usage.py \
        src/spark/bundles src/spark/web/endpoints/bundles.py \
        src/spark/tools/my_files.py src/spark/web/attachments.py \
        src/spark/knowledge/jobs.py src/spark/knowledge/build_jobs.py \
        src/spark/web/endpoints/knowledge.py \
        src/spark/mcp_integration src/spark/web/endpoints/mcp_servers.py \
        src/spark/settings_values.py src/spark/bootstrap \
        src/spark/charts src/spark/projects src/spark/preview \
        src/spark/tools/charts.py src/spark/tools/diagrams.py \
        src/spark/web/endpoints/charts.py src/spark/web/endpoints/projects.py
}

postgres_suites() {
    SPARK_TEST_POSTGRES=required python -m pytest -m "postgres and not integration" \
        -p no:cacheprovider
}

# The uv smoke test, in a scratch home so the user's own Spark data is untouched.
uv_smoke() {
    local home="$work/smoke-home"
    local dist="$work/dist"
    mkdir -p "$home" "$dist"
    uv build --wheel --out-dir "$dist"

    export UV_TOOL_DIR="$work/uv-tools"
    export UV_TOOL_BIN_DIR="$work/uv-bin"
    uv tool install "$dist"/cognisn_spark-*.whl

    local smoke_env=(
        "HOME=$home" "USERPROFILE=$home" "APPDATA=$home/appdata"
        "LOCALAPPDATA=$home/localappdata" "XDG_CONFIG_HOME=$home/config"
        "XDG_DATA_HOME=$home/data" "XDG_STATE_HOME=$home/state"
        "PATH=$UV_TOOL_BIN_DIR:$PATH"
    )
    env "${smoke_env[@]}" spark migrate

    env "${smoke_env[@]}" SPARK__INTERFACE__PORT="$web_port"         SPARK__INTERFACE__OPEN_BROWSER=false spark >"$work/spark.log" 2>&1 &
    echo $! >"$work/spark.pid"
    smoke_pid="$(cat "$work/spark.pid")"

    local up=0
    for _ in $(seq 1 90); do
        if ! kill -0 "$smoke_pid" 2>/dev/null; then
            echo "Spark process exited early"
            break
        fi
        if curl -fsS -o /dev/null "http://127.0.0.1:${web_port}/login"; then
            echo "Spark is serving on :${web_port}"
            up=1
            break
        fi
        sleep 2
    done
    kill "$smoke_pid" 2>/dev/null || true
    smoke_pid=""
    if [ "$up" -ne 1 ]; then
        echo "=== spark.log ==="
        cat "$work/spark.log"
        return 1
    fi
}

container_image() {
    local version
    version="$(cat src/spark/_version.txt)"
    docker build --build-arg "VERSION=${version}" -t "$image" "$root"
    docker image inspect --format '{{.Size}}' "$image" \
        | awk '{printf "image size: %.0f MB\n", $1/1000000}'
    bash deploy/check-image.sh "$image"
}

python -c "import spark" >/dev/null 2>&1 \
    || { echo "wave-gate: activate the project's virtual environment first" >&2; exit 2; }
python -c "import pathlib, spark; assert pathlib.Path(spark.__file__).resolve().is_relative_to(pathlib.Path.cwd().resolve())" \
    >/dev/null 2>&1 \
    || { echo "wave-gate: spark does not import from this checkout" >&2; exit 2; }

step "black" python -m black --check src/ tests/ scripts/ deploy/
step "isort" python -m isort --check-only src/ tests/ scripts/ deploy/
step "mypy strict" mypy_strict
step "licence audit" python scripts/licence_audit.py
pytest_step "sqlite suite" "not postgres and not integration" SPARK_TEST_POSTGRES=
pytest_step "postgresql suites" "postgres and not integration" SPARK_TEST_POSTGRES=required
step "uv smoke" uv_smoke
step "container image" container_image

echo
echo "=== wave-gate: summary ==="
printf '%s\n' "${summary[@]}"
echo "wave-gate: green in $((SECONDS - overall_start)) s"
