# Task 2: PostgreSQL schema migration
## Session: 1 | Complexity: medium

### What I did
- Replaced aiosqlite with psycopg + psycopg-pool in pyproject.toml
- Rewrote db.py: full PostgreSQL DDL with all 10 spec tables, AsyncConnectionPool lifecycle (init_pool/close_pool/get_pool), idempotent seed via ON CONFLICT
- Created models.py with dataclasses for all DB entities (Repo, Prompt, PipelineDef, Pipeline, PipelineStage, AgentSession, SessionLog, Escalation, HtnTask, HtnTaskDep)
- Updated main.py lifespan to init/close the pool
- Converted all routes (dashboard, repos, prompts) from aiosqlite to psycopg: `?` → `%s`, `get_db(DB_PATH)` → `pool.connection()`, removed SQLite-specific pragmas
- Rewrote all tests for pytest-postgresql: conftest.py provides `pg_dsn` and `initialized_db` fixtures using session-scoped PostgreSQL process + per-test fresh database
- Removed DB_PATH from config.py (only DATABASE_URL needed now)

### Learnings
- psycopg pool's `kwargs={"row_factory": dict_row}` configures connections at runtime, but mypy sees the generic type. Used `type: ignore[assignment]` annotations where needed for dict access on fetchone results
- pytest-postgresql 8.0 uses psycopg 3 natively — `factories.postgresql()` returns a sync psycopg.Connection. For async code, extract DSN from `connection.info` attributes and pass to our own `init_db(dsn)` which creates the async pool
- psycopg's `AsyncConnectionPool` needs `open=False` + explicit `await pool.open()` for async initialization
- The pool's `connection()` context manager handles checkout/return automatically — no manual try/finally needed unlike the old aiosqlite pattern
- PostgreSQL DDL uses `SERIAL PRIMARY KEY` instead of `INTEGER PRIMARY KEY AUTOINCREMENT`, `TIMESTAMPTZ` instead of `TEXT` for dates, `DEFAULT now()` instead of `datetime('now')`

### Postcondition verification
- [PASS] ruff check src/ tests/ — 0 errors
- [PASS] mypy src/ --ignore-missing-imports — 0 errors
- [PASS] pytest tests/ -v — 21/21 pass

### Open Questions
- None — clean migration, all tests green
