# Task 55: Local checkpoint commits + head_rev advancement after impl_task completion
## Session: 1 | Complexity: medium

### What I did
- Audited the spec and the impl_task stage and found three coupled gaps
  that together silently broke the ReviewCoversHead invariant in
  production:
    1. `complete_task(task_id, None, "")` — `htn_tasks.checkpoint_rev`
       and `htn_tasks.diary_entry` were never set after a successful
       task. The DB columns existed and the planner accepted them, but
       no caller filled them in.
    2. `pipelines.head_rev` was read by every stage runner
       (`code_review`, `validation`, `impl_task`, `impl_plan`,
       `spec_author`) but written by none of them. So
       `head_rev or review_base_rev` always degraded to the baseline,
       and the full-head review at code_review computed an empty diff.
    3. `PipelineConfig.checkpoint_commits: bool = True` existed in
       config and was serialized to `pipelines.config_json`, but no
       code path read it.
- Three coordinated fixes:
    1. `CloneManager.create_checkpoint_commit(clone_path, message)` —
       stages all working-tree changes with `git add -A`, commits with
       an in-process committer identity via `-c user.name=... -c
       user.email=...` (so commits succeed inside fresh clones that
       have no `~/.gitconfig`), returns the new HEAD revision or `None`
       when the workspace was already clean. Includes a post-staging
       re-check (`diff --cached --name-only`) so adding only ignored
       files does not produce an empty commit.
    2. Three helpers in `stages/impl_task.py`:
       - `_checkpoint_enabled(config_json)` — tolerant parser for the
         per-pipeline opt-out; defaults True so the production-safe
         case wins on missing/malformed config.
       - `_maybe_create_checkpoint(...)` — orchestrates the success
         path: short-circuits on disabled / missing path / GitError,
         calls into the CloneManager, updates `pipelines.head_rev` only
         when a real commit was made.
       - `_build_diary_entry(...)` — synthesizes a structured Markdown
         diary (task name, complexity, retry count, postcondition
         PASS/FAIL list, checkpoint revision) so `htn_tasks.diary_entry`
         is always populated.
    3. `run_impl_task_stage` now accepts `clone_manager: CloneManager |
       None = None` (matching the existing `htn_planner` injection
       pattern), reads `config_json.checkpoint_commits` once at start,
       and on the postcondition success path calls
       `_maybe_create_checkpoint` + passes `(checkpoint_rev, diary)` to
       `planner.complete_task`.

### What I learned
- **Read-only config flags are an anti-pattern with a half-life.**
  `PipelineConfig.checkpoint_commits` had been in the codebase since
  task 8 (config module) but no caller read it. It probably looked like
  forward-compat ergonomics — "we'll need it eventually" — but in
  practice it just lurked. By the time I came to wire it up I had to
  audit every usage to confirm nothing else assumed a stricter contract.
  Cheaper rule: do not commit config knobs that nothing reads. If you
  need a knob in the *spec*, leave it in the spec; only put it in code
  the day it has a reader.
- **GitError must not poison the success path.** I almost wrapped the
  whole completion block in a try/except that re-raises. That would
  have meant: a misconfigured clone (e.g. someone deleted `.git/`)
  causes every task to fail, escalations pile up, and the dashboard
  fills with red. The right move is the inverse: postcondition success
  is the dominant signal; checkpoint failure logs and degrades to
  "no checkpoint" but the task still completes and unblocks dependents.
  The recoverable failure mode is "head_rev never advanced, code review
  sees empty diff, human notices and re-clones." That's louder than
  "tasks succeed but nothing visible changes" but quieter than
  "pipeline halts because git is sad."
- **Testing the `git -c` identity path needs a hostile environment.**
  My first test for "works without global git identity" only stripped
  the per-clone `user.email`/`user.name`, but git happily fell back to
  whatever `~/.gitconfig` had. That made the test pass on machines
  where git was configured (most dev boxes) and silently weak on
  fresh CI. The fix is `monkeypatch.setenv` to redirect HOME, set
  `GIT_CONFIG_GLOBAL` to a nonexistent file, and `GIT_CONFIG_SYSTEM` to
  `/dev/null`. Once git really has no identity, the test exercises the
  `-c` flags. Same shape will show up in any test that exercises
  "the harness brings its own context" — verify the harness wins by
  actively starving the environment.
- **Tests for stage-runner integrations need real on-disk clones, not
  string paths.** The existing `pool_with_stage` fixture seeds
  `clone_path='/tmp/test-clone-impl'` (a string). My initial checkpoint
  tests passed `cmgr` mocks and asserted `create_checkpoint_commit`
  was called — and they failed because my own short-circuit
  (`if not Path(clone_path).exists(): skip`) caught the missing dir
  before reaching the mock. Two responses available: weaken the guard
  (bad — production needs it) or seed real dirs in the test
  (correct). Wrote `_seed_clone_dir_for_pipeline` helper. The lesson:
  the safety valve in production code is also a *boundary* in tests,
  and tests should cross the same boundary explicitly rather than
  routing around it.

### Files touched
- `src/build_your_room/clone_manager.py` — new
  `create_checkpoint_commit` method (stage + commit-with-identity +
  re-check + return new HEAD).
- `src/build_your_room/stages/impl_task.py` — `_checkpoint_enabled`,
  `_maybe_create_checkpoint`, `_update_pipeline_head_rev`,
  `_build_diary_entry` helpers; `clone_manager` injectable on
  `run_impl_task_stage`; success path now passes
  `(checkpoint_rev, diary)` to `planner.complete_task`.
- `tests/test_clone_manager.py` — new `TestCreateCheckpointCommit`
  class (5 tests).
- `tests/test_impl_task.py` — new `TestCheckpointEnabledHelper`
  (6 tests), `TestDiaryEntry` (3 tests), `TestCheckpointCommitWiring`
  (7 tests); `_seed_clone_dir_for_pipeline` helper.
- `docs/plans/build-your-room-tasks.md` — added Phase 20 / Task 55.

### Verification
- `uv run ruff check src/ tests/` — clean
- `uv run mypy src/ --ignore-missing-imports` — clean (no new issues)
- `uv run pytest -q` — 1294 passed (was 1273; +21 new), 0 warnings,
  ~65 s
- Server boot smoke: lifespan succeeds, db init OK, recovery OK,
  orchestrator initialized.
