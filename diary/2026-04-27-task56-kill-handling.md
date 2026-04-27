# Task 56: Wire kill_pipeline through RecoveryManager.handle_kill
## Session: 1 | Complexity: medium

### What I did
- Audited the spec and the orchestrator and found that `kill_pipeline`
  was a near-empty shell. Spec line 540 says kill must "Immediately
  terminate live sessions, snapshot, reset, release HTN claims, and
  mark the pipeline killed", but the implementation only cancelled the
  asyncio task and set `pipelines.status='killed'`. No drain wait, no
  workspace snapshot, no clone reset, no HTN claim release. The full
  cascade already existed inside `RecoveryManager.handle_cancellation`,
  but the kill path silently bypassed it — quietly violating
  WorkspaceMatchesHeadUnlessOwned in production whenever an operator
  hit the kill button on a pipeline mid-stage.
- Three coordinated fixes:
    1. Refactored `RecoveryManager.handle_cancellation` to delegate to
       a new shared `_terminate_pipeline(pipeline_id, *,
       terminal_status, force_snapshot)` helper. Cancel keeps
       `force_snapshot=False` (workspace_state hint is authoritative
       for the cooperative cancel path).
    2. New `RecoveryManager.handle_kill(pipeline_id, owner_token=None)`
       that calls `_terminate_pipeline(terminal_status='killed',
       force_snapshot=True)`. Kill forces the snapshot regardless of
       what `workspace_state` says because the cooperative cancel
       boundary — the only place where stage runners sync
       `workspace_state` — never executed. So the DB hint is unsafe
       to trust, and we must ask the clone directly via the existing
       `_workspace_appears_dirty` helper.
    3. Added `kill_drain_timeout_sec: float = 5.0` to
       `PipelineOrchestrator.__init__`; `kill_pipeline` now sets the
       cancel_event, cancels the asyncio task, awaits with a timeout
       so a hung SDK adapter cannot indefinitely block the cascade,
       then calls `recovery_manager.handle_kill`.

### What I learned
- **"Mark the row killed" is not the same as "kill the pipeline".**
  The original `kill_pipeline` looked superficially complete because
  the DB column flipped to `killed` and the dashboard turned red. But
  HTN claims stayed `in_progress`, sessions stayed `running`, and the
  workspace stayed dirty without a snapshot. From a database row
  point of view this is "killed"; from a spec invariant point of view
  it is a slow-burn corruption — the next pipeline that wants those
  HTN tasks finds them claimed by a token that nobody owns. Status
  columns are not the *truth* of a state machine; cascades are.
- **Cancel and kill are not the same shape, but they share most of
  the cascade.** I almost wrote `handle_kill` as a copy of
  `handle_cancellation` with a different terminal status. The
  difference that matters is exactly one bit: kill must force the
  snapshot, cancel must not. Everything else (release claims, mark
  sessions, mark stages, log line, close LogBuffer) is identical.
  Extracting `_terminate_pipeline(*, terminal_status, force_snapshot)`
  meant the kill path could not silently drift away from the cancel
  path next time the cascade gets extended.
- **Hung adapters are the dominant failure mode for kill.** The whole
  point of pressing kill is that the cooperative cancel didn't work
  — usually because an SDK call is stuck in a network read. So the
  drain step *must* have a timeout. I caught myself writing
  `await task` with no `wait_for`, which would have made kill
  literally indistinguishable from cancel under load. The timeout
  also has a subtle test failure mode: a hung task with
  `asyncio.shield` will not respond to `task.cancel()`, so the
  `wait_for` is what actually rescues the operator. Test
  `test_kill_pipeline_drain_timeout_does_not_block` exercises exactly
  that scenario with `kill_drain_timeout_sec=0.1` and a shielded
  task, asserting return within 2s.
- **`kill_pipeline` has three production scenarios, not one.** The
  obvious case is "active task in this process". The two others are
  "task is hung" (drain timeout) and "server restarted, no active
  task to drain" (skip drain, still cascade). Missing the third
  would have been the most embarrassing bug: someone restarts the
  server, sees a stuck `running` pipeline, hits kill, the dashboard
  flips to `killed` — but HTN claims are still locked. The fix is a
  tiny null-check on `self._active_pipelines.pop(pipeline_id, None)`,
  but the test (`test_kill_pipeline_works_without_active_task`)
  exists because I almost shipped without it.
- **Property tests need fresh names per Hypothesis example.**
  `TestKillInvariants::test_kill_releases_all_in_progress_claims`
  parametrizes over n_in_progress and starting_status, so Hypothesis
  re-runs the test body multiple times within a single pytest test.
  The first version reused `_seed_pipeline` which hardcoded
  `'test-repo'` / `'test-pipeline-def'` — every example after the
  first hit a UNIQUE constraint violation. Fix is the same shape used
  in `test_lease_acquire_release_reacquire_cycle`: inline the seed
  logic with `uuid.uuid4().hex[:8]` suffixes for the repo name, def
  name, and claim tokens. The general lesson: any helper that creates
  uniquely-named DB rows is unsafe inside a Hypothesis test body
  unless the helper takes a uniqueness suffix.

### Files touched
- `src/build_your_room/recovery.py` — refactored `handle_cancellation`
  to delegate to new `_terminate_pipeline`, added `handle_kill`,
  added shared cascade helper.
- `src/build_your_room/orchestrator.py` — added
  `kill_drain_timeout_sec` parameter to `__init__`; rewrote
  `kill_pipeline` to drain the asyncio task with timeout then call
  `recovery_manager.handle_kill`.
- `tests/test_recovery.py` — new `TestHandleKill` class (8 tests)
  including `test_force_snapshot_even_when_workspace_state_clean`
  which uses a real `_init_git_repo` clone and asserts `patch.diff`
  captures the uncheckpointed work and the clone resets to baseline.
- `tests/test_orchestrator.py` — 5 new tests inside
  `TestPipelineLifecycle` plus a new `TestKillInvariants` class with
  one Hypothesis property test.

### Verification
- `uv run ruff check src/ tests/` — clean
- `uv run mypy src/ --ignore-missing-imports` — clean (no new issues)
- `uv run pytest -q` — 1308 passed (was 1294; +14 new), 0 warnings,
  ~73 s
- Server boot smoke: lifespan succeeds, db init OK, recovery OK,
  orchestrator initialized.
