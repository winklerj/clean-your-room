# Task 4: CloneManager for repo cloning, workspace refs, cleanup, and reset-to-head behavior
## Session: 1 | Complexity: medium | Date: 2026-04-09

### What I did
- Created `src/build_your_room/clone_manager.py` with CloneManager class and supporting types
- Implemented async git subprocess helper (`_run_git`) with typed GitError exceptions
- CloneManager methods: create_clone, get_current_rev, is_workspace_clean, reset_to_rev, create_workspace_ref, capture_dirty_diff, cleanup_clone, cleanup_completed_clones, ensure_pipeline_dirs
- Created 28 tests in `tests/test_clone_manager.py` covering all methods and error paths

### Learnings
- `asyncio.create_subprocess_exec` returncode is `int | None`, needs a fallback for mypy (`proc.returncode or 1`) when we know the process has exited
- The constructor accepts injectable `clones_dir` and `pipelines_dir` paths (defaulting to config constants), which makes testing much cleaner without needing to monkeypatch module-level constants
- Test fixtures that seed DB rows need unique names (uuid suffix) when multiple pipelines are created in a single test, because `pipeline_defs.name` has a UNIQUE constraint
- For workspace operations that don't touch the DB (get_current_rev, is_workspace_clean, etc.), `CloneManager.__new__` skips `__init__` to avoid needing a pool — keeps tests fast and focused
- `git diff baseline_rev` captures both staged and unstaged tracked changes; untracked files need separate `git ls-files --others --exclude-standard`
- `git reset --hard rev` + `git clean -fd` is the correct pair for full workspace reset (reset handles tracked files, clean handles untracked)

### Verification
- [PASS] ruff check: All checks passed
- [PASS] mypy: Success, no issues found in 14 source files
- [PASS] pytest: 96 passed (28 new clone_manager tests + 68 existing)
