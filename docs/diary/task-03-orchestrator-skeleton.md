# Task 3: PipelineOrchestrator Skeleton
## Session | 2026-04-09

### What I did
- Created `stage_graph.py` with frozen dataclass types (`StageNode`, `StageEdge`, `ReviewConfig`, `StageGraph`) and graph navigation (`from_json`, `resolve_next_stage` with visit-count tracking and max_visits enforcement)
- Created `orchestrator.py` with the full `PipelineOrchestrator` class:
  - Pipeline lifecycle: `start_pipeline`, `cancel_pipeline`, `kill_pipeline`, `resume_pipeline`
  - Core loop: `_run_pipeline` walks the stage graph, `_run_stage` creates execution rows and dispatches to adapters
  - Durable leases: atomic acquire via `UPDATE ... WHERE owner_token IS NULL OR lease_expires_at < now()`, heartbeat loop, release
  - Startup reconciliation: scans `running` rows with expired leases, snapshots dirty workspaces, releases claims, downgrades to `needs_attention`
  - Dirty workspace recovery: `_snapshot_dirty_workspace` writes metadata to `state/recovery/`
  - Cancellation: cooperative via `asyncio.Event`, snapshots dirty state, resets claims to `ready`
  - Escalation: creates escalation rows, pauses pipeline
- Wired orchestrator into `main.py` lifespan with `reconcile_running_state()` on startup
- 27 stage_graph tests + 20 orchestrator tests (68 total suite)

### Learnings
- psycopg's `dict_row` gives dict-like row access at runtime but mypy sees `tuple` — need `# type: ignore[assignment]` pattern with explicit `dict[str, Any] | None` annotations (matches existing route code pattern)
- The stage graph's `resolve_next_stage` returns a tuple of (next_key, edge) which neatly handles three cases: normal transition, exhausted-with-escalation, and no-match — the caller just pattern-matches
- Visit counts persisted in `recovery_state_json` enable correct back-edge budget tracking across process restarts
- Agent adapters are not yet implemented (Tasks 10-12), so `_run_stage` returns default results based on stage type — this lets the full pipeline loop and state machine be tested end-to-end without real agents
- The heartbeat loop runs as a child task of the pipeline task, so it's automatically cleaned up when the pipeline finishes or is cancelled

### Postcondition verification
- [PASS] ruff check src/ tests/ — all clean
- [PASS] mypy src/ --ignore-missing-imports — 0 errors
- [PASS] pytest tests/ -v — 68/68 passed

### Open Questions
- CloneManager (Task 4) will need to integrate with `_snapshot_dirty_workspace` for the actual VCS diff/patch capture
- Should the orchestrator own the asyncio.Task lifecycle for pipelines, or should routes manage it?
