# Project configuration
- Kvasir library: spark
- Issue tracker: github
- GitHub repository: Cognisn/spark
- Default branch: main
- Release branch style: release/v<version>
- Workstream branches: <kind>/<version>-<aspect>, where <version> is the bare release version with no `v` (for example `feat/0.3.0-file-areas` belongs to `release/v0.3.0`)
- Merge to main: squash
- Commit scopes: llm, core, debate, panel, tools, mcp, skills, knowledge, index, scheduler, daemon, safety, voice, db, web, deps, ci, docs, dev, release
- Shared modules: pyproject.toml, src/spark/database/schema.py, src/spark/core/conversation_manager.py, src/spark/web/server.py, .github/workflows/

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

## Running locally

Run headless with `SPARK__INTERFACE__OPEN_BROWSER=false` and `SPARK__INTERFACE__PORT` set to the `web` port of your sandbox. sentence-transformers downloads its embedding model on first use, so the first run needs network access.

The worktree recipe is `dev/worktree.json`.
