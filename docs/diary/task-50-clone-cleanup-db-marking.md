# Task 50: Mark pipeline as "cleaned" in DB after clone cleanup
## Session: 50 | Complexity: small | Phase: 15

### What I did
- Added `clone_cleaned_at TIMESTAMPTZ` column to the pipelines schema
- Changed `clone_path` from `TEXT NOT NULL` to `TEXT` (nullable) to allow NULL after cleanup
- Updated per-pipeline cleanup (`cleanup_pipeline_clone`) to SET clone_path=NULL, clone_cleaned_at=now() after shutil.rmtree
- Updated bulk cleanup (`cleanup_completed_clones`) with the same DB marking
- Updated API cleanup route (`routes/api.py`) with the same DB marking
- Dashboard `terminal_count` now excludes already-cleaned pipelines (clone_path IS NOT NULL)
- Pipeline card shows "Clone cleaned" badge when cleaned, hides cleanup button
- Pipeline detail page shows cleaned state with timestamp, hides clone path/copy button/size
- Added CSS for cleaned badge styling
- Fixed `_get_clone_size` to handle None clone_path from DB

### Learnings
- The `SELECT p.*` pattern in data-fetching functions means new columns automatically propagate to templates without changing queries — a good architectural choice from earlier phases
- The bulk cleanup route already had `AND clone_path IS NOT NULL` in its WHERE clause, which naturally excludes already-cleaned pipelines from re-processing
- Stage runners, recovery, and orchestrator all only operate on active (non-terminal) pipelines, so making clone_path nullable doesn't affect them — they never see cleaned pipelines
- Idempotent cleanup is important: calling cleanup twice on the same pipeline should succeed (the second call finds clone_path=NULL, skips rmtree, still updates clone_cleaned_at)

### Postcondition verification
- [PASS] Schema updated: clone_cleaned_at column added, clone_path nullable
- [PASS] Per-pipeline cleanup writes DB: clone_path=NULL, clone_cleaned_at=now()
- [PASS] Bulk cleanup writes DB for all cleaned pipelines
- [PASS] API cleanup writes DB
- [PASS] Dashboard excludes cleaned from terminal count
- [PASS] Pipeline card shows cleaned badge
- [PASS] Pipeline detail shows cleaned state
- [PASS] lint_clean: ruff check passes
- [PASS] type_check: mypy passes
- [PASS] tests_pass: 1195 tests, 0 warnings
