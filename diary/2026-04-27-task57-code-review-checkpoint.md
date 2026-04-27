# Task 57: Code review checkpoint commits + head_rev advancement after fix turns
## Session: 1 | Complexity: medium

### What I did
- Spec lines 866-867 require: "After fixing, update `head_rev` and loop
  back to review the same full diff range." I traced the actual code
  in `stages/code_review.py` and found a silent no-op:
    1. At stage entry, `head_rev` is read from the pipeline row once
       and never re-read.
    2. After `_run_fix_agent(...)` returns, the fix-agent's edits are
       sitting uncommitted in the working tree.
    3. The next-iteration `_capture_full_diff(clone_path,
       review_base_rev, head_rev)` is called with the *original*
       `head_rev` — so round 2 captures byte-for-byte the same diff as
       round 1, and the reviewer sees no evidence the fix happened.
- Three coordinated fixes mirroring the impl_task pattern from task 55:
    1. `run_code_review_stage` accepts an injectable `clone_manager:
       CloneManager | None = None` (default constructs from pool to
       match `htn_planner`/`session_runner` injection patterns) and
       reads `_checkpoint_enabled(pipeline.config_json)` once at start.
    2. New `_maybe_create_review_checkpoint(...)` runs after each
       `_run_fix_agent` and before the next `_capture_full_diff`.
       Three short-circuit guards (disabled / clone path missing /
       GitError) match the impl_task safety valve set. On success it
       updates `pipelines.head_rev` and returns the new rev so the
       in-memory `head_rev` advances.
    3. Helpers added in a new "Checkpoint helpers" section at the
       bottom of `stages/code_review.py`: `_checkpoint_enabled`,
       `_maybe_create_review_checkpoint`, `_update_pipeline_head_rev`.

### What I learned
- **A "loop" can be silently a fixed-point even when every line
  executes.** Round 2 ran. `_run_fix_agent` ran. `_capture_full_diff`
  ran. The reviewer ran. *Every single statement executed and
  produced a result.* But the diff input was identical to round 1's,
  so the reviewer's verdict converged to whatever round 1 said
  (modulo non-determinism). No exception, no log line, no test
  failure — just a loop that wasn't really iterating. The detection
  rule I want to internalize: when the spec says "loop until X,"
  audit what *changes* between iterations. If the inputs to the loop
  body don't visibly mutate, the loop body isn't really iterative.
- **Duplication can be load-bearing for stage isolation.**
  `_checkpoint_enabled` is now in both `stages/impl_task.py` and
  `stages/code_review.py`. The DRY instinct says factor it into a
  shared `stages/_checkpoints.py`. I resisted. Reasoning: each stage
  module today is its own self-contained verb (you can read
  `stages/impl_task.py` end-to-end and understand the verb without
  jumping); the helper is 12 lines; both copies are independently
  tested. Sharing would couple their evolution (a change to the
  config schema for one stage would touch the other), and the only
  thing they have in common semantically is "stages care about the
  same opt-out flag." When a *third* stage needs it, that's the
  signal — not the second. Until then, two short copies beat a
  premature module.
- **The load-bearing test is the one that makes the assertion the
  bug needs.** I wrote 7 integration tests, but only one of them
  (`test_recapture_diff_uses_new_head_rev`) would have failed
  *before* this fix in a way that names the bug. The others all
  exercise reasonable surface area but they're scaffolding around
  the load-bearing assertion: round 2's `_capture_full_diff` is
  called with `newhead-2`, not `def456`. If I had to pick one test
  to keep, that's it; the rest are insurance against regressions in
  the surrounding plumbing. Worth labeling these mentally as
  load-bearing vs. supporting so future-me knows which one to read
  first when this stage breaks.
- **Test seeding follows the production safety valve, not around
  it.** Same lesson as task 55, restated to make sure I learned it:
  `_maybe_create_review_checkpoint` short-circuits when the clone
  path doesn't exist on disk. The shared `pool_with_stage` fixture
  seeds `clone_path='/tmp/test-clone-cr'` (a string). My initial
  test asserted `cmgr.create_checkpoint_commit.assert_called_once()`
  and failed because my own guard caught the missing dir before
  reaching the mock. The tempting fix is to weaken the guard (bad —
  production needs it). The right fix is to seed a real on-disk
  directory in tests via `_seed_clone_dir_for_pipeline(tmp_path)`.
  And keep one test (`test_checkpoint_skipped_when_clone_path_missing`)
  that *does* leave the synthetic path in place, to verify the
  safety valve fires. Tests should cross the production boundary,
  not route around it.

### Files touched
- `src/build_your_room/stages/code_review.py` — added `CloneManager,
  GitError` import, `clone_manager` parameter on
  `run_code_review_stage`, `cmgr`/`checkpoint_enabled` initialization,
  post-`_run_fix_agent` checkpoint call before next
  `_capture_full_diff`, "Checkpoint helpers" section with
  `_checkpoint_enabled`, `_maybe_create_review_checkpoint`,
  `_update_pipeline_head_rev`.
- `tests/test_code_review.py` — added `_checkpoint_enabled` import,
  `_make_mock_clone_manager` and `_seed_clone_dir_for_pipeline`
  helpers, `TestCodeReviewCheckpointEnabledHelper` (5 unit tests),
  `TestCodeReviewCheckpointWiring` (7 integration tests including
  the load-bearing `test_recapture_diff_uses_new_head_rev`).
- `docs/plans/build-your-room-tasks.md` — added Phase 22 / Task 57.

### Verification
- `uv run ruff check src tests` — clean
- `uv run mypy src` — clean (no new issues)
- `uv run pytest -q` — 1320 passed (was 1308; +12 new), 0 warnings,
  ~74 s
- Server boot smoke: lifespan succeeds, db init OK, recovery OK,
  orchestrator initialized, GET / returns 200.
