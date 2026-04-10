# Task 38: Extract recovery into recovery.py

**Date:** 2026-04-10
**Phase:** 7 — Architecture Alignment

## What was done

Extracted all recovery-related logic from `orchestrator.py` into a dedicated `RecoveryManager` class in `recovery.py`, following the same extraction pattern established by Task 37 (LeaseManager).

### RecoveryManager responsibilities
- `reconcile_running_state()` — startup scan of stale 'running' pipelines with expired leases; downgrades to 'needs_attention', fails stages, interrupts sessions, releases HTN task claims, creates escalations
- `snapshot_dirty_workspace()` — captures uncheckpointed edits into `state/recovery/{timestamp}/` with JSON metadata; updates DB workspace_state and dirty_snapshot_artifact
- `handle_cancellation()` — cooperative cancellation: snapshots dirty workspaces, releases HTN claims, cancels sessions/stages, marks pipeline cancelled, closes log buffer
- `load_visit_counts()` — static helper to parse edge visit counts from recovery_state_json with defensive fallback to empty dict

### Orchestrator changes
- Added `recovery_manager` constructor parameter (injectable, auto-created if not provided)
- All four recovery methods delegate to `_recovery_manager`
- Thin backward-compatible delegate methods preserved for existing callers
- Removed unused imports (`datetime`, `timezone`, `Path`, `PIPELINES_DIR`)

### Testing
- 33 new tests in `test_recovery.py`: 7 reconciliation, 4 snapshot, 8 cancellation, 7 visit count loading, 4 orchestrator delegation, 4 property-based (PBT)
- Updated `test_orchestrator.py` dirty workspace test to inject RecoveryManager with custom pipelines_dir instead of patching module-level PIPELINES_DIR
- All 1096 tests pass (1063 existing + 33 new)

## Learnings

1. **Injectable dependencies over module patching**: The LeaseManager extraction taught us to make dependencies injectable in the constructor. For RecoveryManager, this meant adding `pipelines_dir` as a keyword-only parameter with a default. The dirty workspace test in test_orchestrator.py previously monkey-patched `orch_mod.PIPELINES_DIR` — this was fragile because after extraction, the module-level constant wasn't used by the delegating orchestrator anymore. Injecting a RecoveryManager with a custom `pipelines_dir` is cleaner and doesn't break when the internal delegation chain changes.

2. **Shared static methods can live on either class**: `load_visit_counts` is called both by the orchestrator's `_run_pipeline` (to initialize visit counts) and referenced through `_load_visit_counts`. Making it a static method on RecoveryManager and having the orchestrator delegate works cleanly — no instance state needed.

3. **LogBuffer dependency**: RecoveryManager needs the LogBuffer for `handle_cancellation` (to append "Pipeline cancelled" and close the buffer). This creates a shared dependency between orchestrator and recovery manager. Since both are constructed at the same time and share the same LogBuffer instance, this is fine — but it means RecoveryManager isn't purely a data-layer component like LeaseManager.
