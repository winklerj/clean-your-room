# Task 51: Wire CloneManager into orchestrator pipeline lifecycle

## Session: 1 | Complexity: medium | Tests: 1204 total (9 new)

### What I did
- Added CloneManager as injectable dependency on PipelineOrchestrator constructor
- Added `_ensure_clone()` method that checks clone_path existence before cloning
- Wired `_ensure_clone` as the first step in `_run_pipeline`, before lease acquisition
- Updated test seed helpers to ensure clone directories exist for existing tests

### Learnings
- The orchestrator had CloneManager.create_clone() available since Task 4 but it was
  never called from the pipeline lifecycle. All stage runners read pipeline["clone_path"]
  from the DB and would have received empty string "" at runtime.
- The spec clearly defines the lifecycle order: clone -> capture review_base_rev -> acquire
  lease -> follow stage graph (lines 496-498). The implementation skipped steps 1-2.
- When adding a check like `Path(clone_path).exists()` to existing code, consider that
  test seed helpers may need updating. Tests that seeded pipelines with fake clone_paths
  (like "/tmp/test-clone") now need those directories to exist because _ensure_clone
  checks for them. The fix: create the directory in the seed helper.
- For e2e tests with mocked CloneManager, the mock's `create_clone` needs a side_effect
  that updates the DB (just like the real implementation does) — otherwise the pipeline
  loads stale DB state with clone_path="" and stages can't find the clone.
- The dependency injection pattern (optional parameter defaulting to real instance) used
  consistently for LeaseManager, RecoveryManager, and now CloneManager makes testing
  straightforward — inject a MagicMock(spec=CloneManager) and verify call patterns.

### Open Questions
- None — this was a clear gap in the spec-to-implementation mapping.
