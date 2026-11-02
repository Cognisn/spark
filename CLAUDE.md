# Project configuration
- Kvasir library: spark
- Issue tracker: github
- GitHub repository: Cognisn/spark
- Default branch: main
- Release branch style: release/v<version>
- Workstream branches: <kind>/<version>-<aspect>, where <version> is the bare release version with no `v` (for example `feat/0.3.0-file-areas` belongs to `release/v0.3.0`)
- Merge to main: squash
- Commit scopes: llm, core, debate, panel, tools, mcp, skills, knowledge, index, scheduler, daemon, safety, voice, db, web, deps, ci, docs, dev, release
- Shared modules: pyproject.toml, src/spark/db/ (the data layer and the bridge), src/spark/migrations/ (the migration chain), src/spark/mode.py (the mode accessor), src/spark/web/db.py, src/spark/core/conversation_manager.py, src/spark/web/server.py, src/spark/identity/ (permissions, access, audit, runtime, sessions), src/spark/database/scope.py and src/spark/web/scope.py (user scoping), src/spark/tools/registry.py (the tool decision point), tests/conftest.py (shared fixtures), .github/workflows/

# SPARK

Spark (Secure Personal AI Research Kit) is a multi-provider LLM web interface with MCP tool integration, published to PyPI as `cognisn-spark`. This repository is public: no hostnames, drive letters, workstation names, credentials or internal detail in any committed file.

## Commit scopes

| Scope | Covers |
|---|---|
| `llm` | `src/spark/llm/` providers and the model manager |
| `core` | `src/spark/core/` other than debate and panel (application, conversation manager, agents, compaction, updater) |
| `debate` | `src/spark/core/debate/` |
| `panel` | `src/spark/core/panel/` |
| `tools` | `src/spark/tools/` built-in tools |
| `mcp` | `src/spark/mcp_integration/` |
| `skills` | `src/spark/skills/` and bundled skills |
| `knowledge` | `src/spark/knowledge/` knowledge graphs |
| `index` | `src/spark/index/` embeddings and vector indexes |
| `scheduler` | `src/spark/scheduler/` autonomous actions |
| `daemon` | `src/spark/daemon/` system tray daemon |
| `safety` | `src/spark/safety/` prompt inspection |
| `voice` | `src/spark/voice/` |
| `db` | `src/spark/database/` |
| `web` | `src/spark/web/` server, endpoints, templates and static assets |
| `deps` | dependency changes in `pyproject.toml` |
| `ci` | `.github/workflows/` |
| `docs` | `docs/`, `README.md` and tool documentation |
| `dev` | developer tooling: `dev/`, `scripts/`, `.gitattributes`, `.gitignore`, this file |
| `release` | changelog and release mechanics |

Version stamps are unscoped: `chore: stamp <version>`.

## Rules

- Python 3.12 (the minimum supported version; CI also runs 3.13).
- black and isort, at the exact versions pinned in the `dev` extra of `pyproject.toml`, are the formatters. `black --check src/ tests/` and `isort --check-only src/ tests/` must be clean before a push.
- mypy is advisory in CI for existing code.
- The Windows CI test step is advisory (`continue-on-error`); Linux and macOS are the gate, with the uv smoke test.
- Versions follow PEP 440 (`0.3.0a1`, `0.3.0b1`, `0.3.0rc1`). The single source of the version is `src/spark/_version.txt`.
- Stamp with `python scripts/stamp_version.py <version>`. It refuses a version that does not follow the current one, and moves `[Unreleased]` in `CHANGELOG.md` into a dated section. Only the integration session stamps, on the release branch, and commits the result as `chore: stamp <version>`.
- Workstreams merge into the release branch with `--no-ff` merge commits from a green CI tip.
- The release line reaches `main` only as a squash merge at the final version (`chore: release <version>`), followed by the `v<version>` tag and the GitHub release, which publishes the wheel to PyPI. Each of these steps happens only when Matthew instructs it.
- Binary, DMG and NSIS release jobs are disabled; releases are PyPI only.
- Update `CHANGELOG.md` under `[Unreleased]` in the same commit as the change.
- Text files use LF line endings (`.gitattributes`); scripts that write files pass `newline="\n"`.

## Data layer

- Two modes (`spark.mode`): personal (SQLite) and server (PostgreSQL). Ask `spark.mode.current_mode()` or `is_server()`; never read the raw settings.
- Repositories in `src/spark/database/` are async functions taking an `Executor` first; write SQL once with `?` placeholders in the subset SQLite and PostgreSQL share (`spark.db.dialect` for upserts and `RETURNING`). Keep every query's user scoping. A multi-statement unit of work is one repository coroutine using `executor.transaction()`.
- Endpoints await repositories on `spark.web.db.database(request)`. Synchronous code on worker threads uses `SyncDatabase.call(repo.fn, ...)` (`spark.web.db.sync_database(request)` or the bridge it was given), and is started with `asyncio.to_thread` or its own thread, never called directly on the event loop. New code should be async.
- Schema changes are new Alembic revisions in `src/spark/migrations/versions/` (never edits to `schema_0_2_0.py` or the baseline), each with a tested downgrade, on both SQLite and PostgreSQL. Only the integration session renumbers or re-chains revisions.
- Tests: `spark_db` (async, SQLite and PostgreSQL), `sync_db` (the bridge, SQLite) and `tests/db_helpers.app_client` (the web app with its lifespan database). PostgreSQL tests are marked `postgres` and run in testcontainers (`pgvector/pgvector:pg16`); CI's server job sets `SPARK_TEST_POSTGRES=required`. A coroutine that is never awaited fails the suite.
- `python scripts/licence_audit.py` must pass: no strong copyleft runtime dependency; weak copyleft only as a named exception in that script, agreed by Matthew.

## Identity (server mode)

Server mode uses warden (`cognisn-warden` from PyPI) for users, sessions, roles and audit; personal mode keeps its own sign-in and has one local user who holds every permission.

- **Who is asking:** in an endpoint, `spark.identity.access.current_user_guid(request)` is the SPARK user guid for every repository call; `current_identity(request)` gives the `SparkIdentity` (guid, warden user id, email, roles, permissions). Never use `app.state.user_guid` or `get_user_guid` for a user in server mode. A worker or background task takes the owner's guid explicitly and resolves the owner's permissions at run time (`spark.scheduler.owners`).
- **Checking a permission:** declare it once in `spark.identity.permissions.PERMISSIONS` and grant it in `ROLES` (a new role is a new `ROLES` entry, no code change elsewhere). Guard a route with `Depends(require_permission("spark.<capability>"))` (401 signed out, 403 without it) or test with `has_permission(request, name)`. Tool categories are decided only by `spark.tools.registry.is_category_available` with the user's permissions; pass them with `tools_config_for(request, config)` or `request_permissions(request)`. Server mode fails closed: no permissions means refused. `spark.tools.registry.ANY_ROLE` describes the deployment, never a user.
- **Scoping:** every repository read and write takes the user guid after the executor and filters by it; another user's id answers 404 (`spark.web.scope.not_found`). A deliberately unscoped function says so in its docstring and is never reachable from a request.
- **Auditing:** register a new action once in `spark.identity.audit.ACTIONS` (names under `spark.`, never `warden.`). Emit with `await audit.emit_request(request, action, resource_type=..., resource_id=..., details=...)` in an endpoint, `await audit.emit(action, user_guid=...)` in async code, `audit.submit(...)` from a thread, or `await audit.record_in(conn, ...)` inside a SPARK transaction so the row commits or rolls back with the write. Details never carry secrets or content (`scrub` drops them). Every call does nothing in personal mode. warden audits its own sign-in and administration events.
- **Sessions:** warden tokens live in SPARK's cookies (access `SameSite=Strict` on `/`, refresh scoped to `/auth/session/refresh`), refreshed by `spark.identity.web`; CSRF tokens are bound to the warden session and checked by `spark.web.security`; the Origin policy is pinned to `server.public_base_url`, which server mode requires. API keys are bearer only and never mixed with a session cookie.
- **Tests:** server-mode identity tests run on PostgreSQL with warden's chain (`tests/identity_web_helpers.py`, `tests/identity_screen_helpers.py`); mutation-check every new guard. Live SSO tests are marked `integration` and run only when Matthew triggers them.

## Running locally

Run headless with `SPARK__INTERFACE__OPEN_BROWSER=false` and `SPARK__INTERFACE__PORT` set to the `web` port of your sandbox. For server mode, start the sandbox stack (`compose_up` in `ports.txt`), set `SPARK__MODE=server` and `SPARK__DATABASE__URL` to the sandbox's `postgres` port, and run `spark migrate` first. sentence-transformers downloads its embedding model on first use, so the first run needs network access.

The worktree recipe is `dev/worktree.json`.
