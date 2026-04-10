# Task 30: JSON API for Programmatic Access

## What I did
- Created `src/build_your_room/routes/api.py` with all 10 JSON API endpoints from the spec
- Registered the API router in `main.py`
- Wrote 36 comprehensive tests covering all endpoints, edge cases, and error states

## Endpoints implemented
- `GET /api/pipelines` — list with optional status/repo_id filtering
- `POST /api/pipelines` — create a new pipeline (validates def + repo exist)
- `GET /api/pipelines/{id}/status` — rich status summary (stage, HTN progress, cost, escalations)
- `POST /api/pipelines/{id}/cancel` — graceful cancel with state validation
- `POST /api/pipelines/{id}/kill` — force kill with terminal state guard
- `GET /api/pipelines/{id}/tasks` — full HTN task tree with dependencies
- `GET /api/pipelines/{id}/tasks/progress` — task counts by status (primitives only)
- `POST /api/pipelines/{id}/cleanup` — delete clone directory for terminal pipelines
- `GET /api/escalations` — list with optional status filter
- `POST /api/escalations/{id}` — resolve or dismiss with action/resolution body

## Learnings
- DB rows from psycopg with `dict_row` contain native Python `datetime` objects that need
  recursive serialization for `JSONResponse`. A flat `_serialize_row` breaks on nested structures
  like the HTN task tree (children lists). Solution: recursive `_serialize_value` that handles
  `datetime`, `dict`, and `list` types.
- The orchestrator may not be available in test environments (no lifespan). The cancel/kill
  endpoints need a DB-update fallback when `orchestrator is None` to ensure the state transition
  is persisted even without a running orchestrator process.
- Pydantic `BaseModel` request bodies work well for the JSON API — automatic validation,
  clear error messages, and optional fields with defaults.

## Test coverage
- 36 tests: 4 list pipelines, 4 create pipeline, 3 pipeline status, 3 cancel, 3 kill,
  3 task tree, 3 task progress, 4 cleanup, 3 escalation list, 6 escalation resolve/dismiss
- Each test documents its invariant in the docstring
- Edge cases: empty DB, nonexistent resources, terminal state guards, already-resolved escalations

## Stats
- 924 total tests (922 pass, 2 pre-existing flaky property-based failures)
- Lint clean, type check clean
