# Diary: Fork clean-your-room → build-your-room

## Task
Phase 1, Task 1: Rename package, strip GitHub-specific code, strip specs-monorepo pattern, update to local-repo model.

## What I did
- Renamed package from `clean_room` to `build_your_room` across all source and test files
- Removed `git_ops.py` (GitHub clone/pull/specs-monorepo operations)
- Removed `models.py` (GitHubUrl dataclass and parser)
- Removed `runner.py` (single-agent JobRunner — replaced by orchestrator in later tasks)
- Removed `routes/jobs.py` and job-related templates (`job_viewer.html`, `partials/job_status.html`)
- Updated `config.py`: `BUILD_YOUR_ROOM_DIR` replaces `CLEAN_ROOM_DIR`, added all spec env vars (model configs, lease TTL, heartbeat interval, context threshold, etc.)
- Updated `db.py`: New repos table schema (name, local_path, git_url, default_branch, archived). Removed jobs/job_logs tables. Extended prompts table with `body` (was `template`), `stage_type`, `agent_type`. Seeded 4 default prompts per spec.
- Updated `routes/repos.py`: Local-path-based repo management (validates directory exists)
- Simplified `routes/dashboard.py`: Repos list only, no job stats
- Updated `routes/prompts.py`: Uses `body`, `stage_type`, `agent_type` fields
- Updated all templates for "Build Your Room" branding and new data model
- Updated `pyproject.toml`: name → `build-your-room`
- Rewrote all tests for new schema and package name, removed tests for stripped modules

## Learnings
- The `aiosqlite.Row` factory returns row objects that support both index and key access, which makes schema changes straightforward — templates can use `repo['name']` dict-style access
- Keeping SQLite as a transitional DB layer while preparing for PostgreSQL (task 2) avoids a two-step migration in a single task
- The clean-your-room pattern of `from build_your_room.main import templates` (lazy import inside route handlers) avoids circular imports between routes and the main app module — this pattern should be preserved going forward

## Postcondition verification
- [PASS] `uv run ruff check src/ tests/` — all checks passed
- [PASS] `uv run mypy src/ --ignore-missing-imports` — 0 errors
- [PASS] `uv run pytest tests/ -v` — 20/20 tests pass

## Stats
- Files removed: 7 (git_ops.py, models.py, runner.py, routes/jobs.py, job_viewer.html, job_status.html, integration test)
- Tests removed: 5 files (test_git_ops, test_models, test_runner, test_routes_jobs, test_integration)
- Tests remaining: 20 passing across 5 test files
