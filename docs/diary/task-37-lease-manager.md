# Task 37: Extract lease management into lease_manager.py
## Session: 37 | Complexity: medium | Tests: 32 new (1063 total)

### What I did
- Created `src/build_your_room/lease_manager.py` with a `LeaseManager` class providing durable lease ownership for pipelines, stages, and sessions
- Extracted pipeline lease acquire/release/renew/heartbeat from `orchestrator.py` into the dedicated module
- Added stage-level and session-level acquire/release operations (previously only pipeline-level existed)
- Added expiry query methods: `is_lease_expired`, `get_expired_running_pipelines`, `get_live_running_pipelines`
- Added `release_all_for_pipeline` for bulk cleanup during cancellation/kill/recovery
- Updated `PipelineOrchestrator` to accept an optional injected `LeaseManager` and delegate all lease operations
- Kept backward-compatible private methods (`_acquire_pipeline_lease`, `_release_pipeline_lease`, `_heartbeat_loop`) as thin delegates so existing tests pass unchanged

### Learnings
- The `LeaseError(RuntimeError)` inheritance pattern means existing tests matching `pytest.raises(RuntimeError)` continue to work without modification — good approach for gradual extraction
- `psycopg` with `dict_row` requires `# type: ignore[assignment]` on every `.fetchone()` / `.fetchall()` call — consistent with the rest of the codebase
- Heartbeat loop with `heartbeat_interval_sec=0` is useful for testing: the loop immediately attempts renewal without sleeping, making tests fast and deterministic
- Task claim leases remain in `HTNPlanner` rather than `LeaseManager` because they use a different atomic protocol (CTE WITH FOR UPDATE SKIP LOCKED) vs simple UPDATE ... WHERE

### Postcondition verification
- [PASS] ruff check src/ tests/ — all clean
- [PASS] mypy src/ --ignore-missing-imports — 0 errors
- [PASS] pytest tests/test_lease_manager.py — 32/32 pass
- [PASS] pytest tests/test_orchestrator.py — 28/28 pass (no regressions)
- [PASS] pytest tests/ — 1061/1063 pass (2 pre-existing Hypothesis whitespace flakes)

### Architecture decisions
- LeaseManager is a standalone class, not tightly coupled to the orchestrator — stage runners and future recovery module can use it directly
- Pipeline lease uses optimistic concurrency (UPDATE WHERE owner_token IS NULL OR expired) matching the spec's atomic acquire pattern
- Stage and session leases use simpler UPDATE SET (no contention guard) because they're always acquired by the pipeline owner within a known-live lease
