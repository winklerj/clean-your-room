# Developer Guide

## Environment
- **Package manager:** uv (not pip)
- **Python version:** 3.12 (managed by uv via .python-version)
- **Database:** PostgreSQL (local instance, async via psycopg + psycopg-pool)
- **Run commands:** always prefix with `uv run`
- **This repo uses [Jujutsu (`jj`)](https://martinvonz.github.io/jj/) instead of `git`.** Do not use `git` commands.
  Use `jj` equivalents instead (e.g. `jj log`, `jj diff`, `jj new`, `jj describe`, `jj bookmark`).
  Run `jj help` to discover commands; most map intuitively from their git counterparts.

## Common Commands
```bash
uv sync --extra dev                             # Install dependencies
uv run pytest tests/ -v                         # Run all tests (1096 tests)
uv run ruff check src/ tests/                   # Lint
uv run mypy src/ --ignore-missing-imports       # Type check
uv run uvicorn build_your_room.main:app --reload --port 8317  # Run dev server
```

## Database
- PostgreSQL with async `psycopg` connections via `psycopg_pool.AsyncConnectionPool`
- Schema auto-created on startup via `init_db()` in `src/build_your_room/db.py`
- `dict_row` row factory — queries return `dict[str, Any]`
- DSN configured via `DATABASE_URL` env var (default: `postgres:///build_your_room`)
- 10 tables: repos, prompts, pipeline_defs, pipelines, pipeline_stages, agent_sessions, session_logs, escalations, htn_tasks, htn_task_deps

## Testing
- **Property-based tests first** (Hypothesis) — verify invariants over input space
- **pytest-postgresql** fixtures for test DB isolation (ephemeral PG per test)
- **pytest-asyncio** with `asyncio_mode = "auto"` for async tests
- Mock LLM/agent calls in tests — never make real API calls
- Reference `testing.md` for full testing strategy and patterns
- Every test must document its purpose, invariant, and context in its docstring

### Test organization
- `tests/` — all regression, property, and integration tests
- `experiments/` — learning experiments, not part of CI (run with `uv run pytest experiments/ -v`)

### Key testing patterns
- `psycopg` `dict_row` requires `dict[str, Any] | None` type annotations
- Test DB seeding: use uuid-suffixed unique names for rows with UNIQUE constraints
- Hypothesis with `tmp_path`: use `tempfile.TemporaryDirectory` inside the test body (fixture is function-scoped, not reset between examples)
- Hypothesis `text` strategy: exclude `\r` to avoid write_text newline normalization on macOS

## Architecture

### Core components
- **PipelineOrchestrator** (`orchestrator.py`) — stage graph dispatch, dirty-workspace recovery, startup reconciliation, escalation queue
- **LeaseManager** (`lease_manager.py`) — durable lease/heartbeat ownership for pipelines, stages, sessions; expiry queries for recovery
- **RecoveryManager** (`recovery.py`) — startup reconciliation, dirty-workspace snapshot/reset, cancellation cleanup, visit-count persistence
- **StageGraph** (`stage_graph.py`) — frozen dataclass nodes/edges, JSON parsing, edge resolution with visit-count tracking
- **HTNPlanner** (`htn_planner.py`) — task graph CRUD, atomic claims (CTE WITH FOR UPDATE SKIP LOCKED), readiness propagation, postcondition verification
- **ContextMonitor** (`context_monitor.py`) — usage tracking, rotation decisions (CONTINUE vs ROTATE)
- **CloneManager** (`clone_manager.py`) — async git subprocess, clone creation, workspace ops, cleanup
- **WorkspaceSandbox** (`sandbox.py`) — 4 allowed roots, symlink protection, path guard closure
- **CommandRegistry** (`command_registry.py`) — command templates, verifier registry, condition dispatching
- **BrowserRunner** (`browser_runner.py`) — dev-server lifecycle, dev-browser JSON-RPC bridge, graceful degradation

### Agent adapters (`adapters/`)
- **ClaudeAgentAdapter** — Claude SDK live session handles with multi-turn `send_turn`, context usage, snapshot/close
- **CodexAppServerAdapter** — stdio JSON-RPC protocol, subprocess lifecycle, handshake, thread management

### Stage runners (`stages/`)
- **spec_author.py** — spec authoring + optional review loop
- **impl_plan.py** — implementation plan + HTN task graph population from structured output
- **impl_task.py** — claim loop with context rotation, postcondition verification, resume
- **code_review.py** — full-head diff review + bug-fix loop
- **validation.py** — harness-owned verification commands + optional browser validation
- **review_loop.py** — generic bounded feedback cycle with same-session continuation

### Dashboard routes (`routes/`)
- `/` — pipeline cards grid with HTN progress, context usage, cost, escalation banner
- `/escalations` — escalation queue with resolve/dismiss actions
- `/pipelines/{id}` — stage graph viz, HTN task tree, lease health, live logs
- `/pipeline-defs` — pipeline definition builder with node/edge editing
- `/prompts` — prompt CRUD with usage tracking, filtering, clone, template variables
- `/repos` — repo management

### Data flow
```
Pipeline run:
  clone repo → acquire lease → enter stage graph →
  per stage: create stage row → start agent session → send turns →
    check context after each turn → rotate if needed →
    verify artifacts → update head_rev → resolve next edge →
  complete or escalate
```

### Key invariants
- **SideEffectsContained**: agents write only under clone_path + app-owned logs/artifacts/state dirs
- **RunningImpliesOwner**: running resources must have non-null owner token and unexpired lease
- **ValidStageTransition**: next stage comes from explicit graph edges, never numeric +1
- **UniqueTaskClaim**: at most one live lease per primitive HTN task
- **WorkspaceMatchesHeadUnlessOwned**: clean workspace when no live session owns it

## Configuration
- All env vars documented in `src/build_your_room/config.py`
- Per-pipeline runtime config via `PipelineConfig` frozen dataclass (stored as `config_json`)
- Stage-level config in `pipeline_defs.stage_graph_json` nodes

## Style
- `ruff` with `target-version = "py312"`, `line-length = 100`
- Frozen dataclasses for value types
- Async-first with `asyncio` tasks and `psycopg` async pool
- No `Bash`/shell tools in agent sessions — typed harness tools only

# Important

Sneeze when you finish a task
