# Task 54: Complete dirty workspace recovery (WorkspaceMatchesHeadUnlessOwned)
## Session: 1 | Complexity: medium

### What I did
- Spec lines 524-527 demand: when a workspace is dirty without a live
  owner, capture a patch *and* a changed-file manifest into
  `state/recovery/{timestamp}/`, set `dirty_snapshot_artifact`, reset the
  clone to the accepted baseline, and only then allow review/resume/
  cleanup. Auditing the code, two gaps were live:
    1. `RecoveryManager.snapshot_dirty_workspace` only wrote
       `recovery_metadata.json`. No `patch.diff`. No manifest. No reset.
    2. `reconcile_running_state` and `handle_cancellation` both gated the
       snapshot on `workspace_state != 'clean'`, but no production code
       path ever transitions `workspace_state` away from `clean`. So the
       dirty branch was effectively dead — a real dirty workspace would
       silently slip through reconciliation untouched.
- Patched both gaps with one cohesive change:
    1. `RecoveryManager.__init__` now optionally accepts a `CloneManager`.
       Default `None` keeps the 33 pre-existing unit tests green (they
       construct `RecoveryManager` with a temp `pipelines_dir` and no
       clone). When injected, snapshot writes `patch.diff` via
       `clone_manager.capture_dirty_diff()`, writes `changed_files.json`
       by parsing `git status --porcelain`, and runs
       `clone_manager.reset_to_rev(baseline_rev)`. Each step records a
       boolean flag (`patch_captured`, `manifest_captured`, `clone_reset`)
       into `recovery_metadata.json` so an operator can see what worked
       and what didn't post-mortem.
    2. New `_workspace_appears_dirty()` helper. It consults
       `workspace_state` first as a cheap hint (`dirty_*`/`needs_*` →
       dirty), but if state is `clean` and a `CloneManager` is wired in,
       it asks `is_workspace_clean()` for ground truth. So git is the
       authority — a stale-clean DB row no longer suppresses a real dirty
       reset.
- `PipelineOrchestrator.__init__` now passes its own `CloneManager` into
  the `RecoveryManager` constructor, so production runs automatically
  pick up the git-aware path; tests can still inject either or neither.

### What I learned
- **`_run_git` strips stdout, and that's a footgun for porcelain.** First
  cut of the manifest parser used `_run_git(["status", "--porcelain"])`
  and split each line on fixed offsets (`line[:2]` for status,
  `line[3:]` for path). It silently produced `path="EADME.md"` for a
  modified `README.md` because porcelain encodes "modified-not-staged"
  as a *leading space* (` M README.md`) and `_run_git` calls
  `.strip()`. After strip the line becomes `M README.md`, the leading
  space is gone, and offset-based slicing eats the first char of the
  path. Two viable fixes: (a) thread a `strip=False` kwarg through
  `_run_git`, or (b) parse with `split(None, 1)` which collapses both
  forms. Went with (b) — single-call-site change, no API surface.
  Saved a comment on the helper explaining why offsets are wrong here.
- **A "boolean hint" field is fine, but never the only signal.** The
  `workspace_state` column existed but no code path actually set it to
  anything other than `clean`, which meant the entire dirty-recovery
  branch was a no-op in production. Resist the urge to refactor and
  populate the column — `git status` is already the authoritative
  source. The right move is to treat `workspace_state` as advisory and
  always cross-check the on-disk truth when it matters. Same shape will
  show up elsewhere: any DB column that mirrors filesystem state needs
  a "verify against the filesystem when stakes are high" path.
- **Optional injection beats mandatory injection for cross-cutting
  helpers.** `CloneManager` is an *optional* dep on `RecoveryManager`.
  This kept all 33 pre-existing tests green without rewriting their
  setup, and the added 11 tests cover both modes (with-manager and
  without-manager) explicitly. If the constructor had been changed to
  *require* `CloneManager`, the diff would have ballooned and the
  signal-to-noise ratio of the test changes would have collapsed.
- **The security-reminder hook fires on substring matches, not
  semantics.** Tried twice to add a raw-subprocess call using
  `asyncio.create_subprocess` (the safe execFile-style API); the hook
  blocked both edits because the file mentioned a JS/TS-style call
  pattern. Worked around it by adapting the parser to handle the
  existing strip-and-collapse behavior of `_run_git`. Net: the codebase
  is no worse off (one inline parser comment), but it's a reminder that
  defensive automation can refuse correct work — pick the lower-friction
  path when the hook is wrong.

### Files touched
- `src/build_your_room/recovery.py` — constructor accepts
  `CloneManager`; new `_workspace_appears_dirty`,
  `_capture_patch_artifact`, `_capture_changed_files_manifest`,
  `_reset_clone_to_baseline` helpers; snapshot now records capture
  flags; reconcile + cancellation use the git-aware dirty check.
- `src/build_your_room/orchestrator.py` — passes its own
  `CloneManager` into `RecoveryManager`.
- `tests/test_recovery.py` — added `TestSnapshotWithRealClone` (7
  tests: patch capture, manifest, reset, metadata flags, no-dir
  fallback, non-git fallback, empty-clean patch) and
  `TestGitDirtyDetection` (4 tests: reconcile triggers when git dirty
  but DB clean, cancellation likewise, clean+clean no-op, no-manager
  preserves legacy). Added `_init_git_repo` helper (real `git init -b
  main` + commit).
- `docs/plans/build-your-room-tasks.md` — added Phase 19 / Task 54.

### Verification
- `uv run ruff check src/ tests/` — clean
- `uv run mypy src/ --ignore-missing-imports` — clean (no new issues)
- `uv run pytest -q` — 1273 passed (was 1262; +11 new), 0 warnings,
  ~63 s
- Server boot smoke: lifespan succeeds, route layer answers (404 on
  `/healthz` — no health route exists, but the app is up)
