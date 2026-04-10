# Diary

## Goal

Record the implementation of Task 26: Integration tests with mock adapters + pytest-postgresql, covering end-to-end multi-component workflows through the orchestrator with real DB.

## Step 1: Integration test design and implementation

Designed and implemented 20 integration tests in `tests/test_integration.py` that exercise the full orchestrator pipeline lifecycle with real PostgreSQL (via pytest-postgresql) and mock adapters. The key design decision was to patch stage runners at the orchestrator dispatch level rather than wiring up internal stage I/O, which keeps tests focused on cross-cutting orchestrator concerns (stage transitions, lease management, visit counts, escalation flows, cancellation semantics, concurrent isolation) without duplicating per-stage unit test coverage.

The tests span 7 test classes covering full pipeline lifecycle, escalation/cancellation, HTN task lifecycle with real HTNPlanner, stage transitions with back-edges, property-based invariants, artifact handoff chains, and concurrent pipeline isolation.

### Prompt Context

**User prompt (verbatim):** "Study @build-your-room-spec.md thoroughly. Use HTN planning and implement the next incomplete task ready to be implemented from the task list."

**Assistant interpretation:** Identify Task 26 (integration tests) as the next incomplete task and implement it fully.

**Inferred user intent:** Continue building out the test suite toward production readiness, validating that multi-component flows work correctly end-to-end.

### What I did
- Created `tests/test_integration.py` with 20 tests across 7 classes
- Built reusable infrastructure: `FakeTurnResult`, `_make_mock_session`, `_make_mock_adapter`, DB seeding helpers (`_seed_full_pipeline`, `_seed_htn_tasks_for_pipeline`), and query helpers for pipeline/stage/session/escalation/HTN rows
- Defined two stage graph fixtures: full 5-stage graph matching the spec and a minimal 2-stage graph for focused tests
- Fixed 3 ruff lint warnings (unused variable, unused import)
- Updated task list, memory file

### Why
- Per-stage tests already cover individual stage runners in isolation; integration tests validate the orchestrator's stage-graph traversal, lease lifecycle, escalation→resume flow, cancellation semantics, and concurrent pipeline isolation
- Real PostgreSQL via pytest-postgresql ensures DB transactions (CTE WITH FOR UPDATE SKIP LOCKED, etc.) work correctly under realistic conditions

### What worked
- Patching stage runners at `build_your_room.orchestrator.run_*_stage` level is clean and effective — the orchestrator's full loop (lease acquisition, heartbeat, graph traversal, visit counting, status transitions) runs unmodified
- asyncio.wait_for with timeout catches stuck pipelines during testing
- Property-based tests with `suppress_health_check=[HealthCheck.function_scoped_fixture]` work well with pytest-postgresql fixtures
- Using unique `suffix` params in seed helpers avoids UNIQUE constraint collisions across property test examples

### What didn't work
- Initial attempt patched internal stage functions like `_artifact_path` and `_verification_artifact_path` — the latter doesn't exist as a module-level patchable function in validation.py, causing `AttributeError`. Simplified to patching at the dispatch level.
- Resume test initially had the `with patch` context only wrapping the first run — after `resume_pipeline` creates a new asyncio task, that task runs outside the patch scope and falls through to no-adapter skip behavior. Fixed by wrapping both runs in the same patch context.

### What I learned
- For orchestrator integration tests, patching at the dispatch boundary (where `orchestrator.py` calls `run_*_stage`) is the right abstraction level — it tests the orchestrator's own logic without coupling to stage internals
- The resume_pipeline flow creates a brand new asyncio.Task, so patches must span the full resume lifecycle
- Hypothesis `suppress_health_check=[HealthCheck.function_scoped_fixture]` is needed when using pytest fixtures (like `initialized_db`) with `@given` decorators

### What was tricky to build
- Getting the cancel test right required a barrier event to synchronize — the test needs the stage to be "running" before issuing cancel, but the stage runs asynchronously. Used `asyncio.Event` as a barrier that the mock stage sets before sleeping.
- The kill test similarly needs careful timing — `kill_pipeline` pops from `_active_pipelines` and cancels the task, so assertions about the dict state need to happen after the kill.

### What warrants a second pair of eyes
- The cancel test asserts `status in ("cancelled", "cancel_requested")` because the cancel event timing is non-deterministic — the pipeline might not have entered the cancellation handler before the assertion. This is a realistic race condition in the real system too.
- Property-based tests use `id(escalate_at)` in suffix strings for uniqueness, which could theoretically collide across examples though extremely unlikely.

### What should be done in the future
- Task 27 (devbrowser recording integration) could add integration tests that exercise the browser validation path end-to-end
- The pre-existing `test_artifact_write_roundtrip` failure (surrogate characters in Hypothesis text strategy) should be fixed by excluding surrogates from the text strategy

### Code review instructions
- Start at `tests/test_integration.py` — read the module docstring, then infrastructure section (lines 1-310), then each test class
- Key classes: `TestFullPipelineLifecycle`, `TestEscalationAndCancellation`, `TestHTNTaskLifecycle`, `TestStageTransitions`, `TestIntegrationProperties`
- Validate: `uv run pytest tests/test_integration.py -v` (all 20 should pass)
- Check lint: `uv run ruff check tests/test_integration.py`

### Technical details
- Test count: 20 new (825 total, up from 805)
- Test breakdown: 3 lifecycle + 5 escalation/cancel + 2 HTN + 4 stage transition + 3 property-based + 1 artifact chain + 2 concurrent
- All tests use `initialized_db` fixture (pytest-postgresql) for real DB isolation
- Stage runners patched at `build_your_room.orchestrator.run_*_stage` level
- HTN tests use real `HTNPlanner(pool)` with only `verify_postconditions` mocked
