# Task 58 — Diary filesystem writes

## Date: 2026-04-27 | Phase 23 | Spec line 811

### What I did

Wired diary entries to land both in `htn_tasks.diary_entry` (already
implemented) and on disk at `{clone_path}/diary/task-{id}-{slug}.md`
(this task). Spec line 811 mandates both. Before this change only the
DB half existed; future agent sessions had no filesystem artifact they
could `Read`/`Glob` to recover prior-task context.

Three additions in `src/build_your_room/stages/impl_task.py`:

- `_slugify(name)` — ASCII slug, length-bounded, fallback to `"task"`,
  precompiled regex.
- `_maybe_write_diary_file(...)` — best-effort writer with the same
  short-circuit set the checkpoint helpers use (empty path, missing
  on-disk clone, `OSError` logged but never propagated).
- Hook in `run_impl_task_stage`'s success path between
  `_build_diary_entry(...)` and `planner.complete_task(...)`, so the
  file mirrors the DB content verbatim.

16 new tests covering slug edge cases, filesystem failures, and the
end-to-end "DB diary == file diary" invariant.

### Learnings

1. **Pre-checkpoint vs post-checkpoint placement.** I considered three
   orderings: (A) write before `_maybe_create_checkpoint` so the file
   gets committed in the same commit as the agent's changes, (B) write
   after but commit separately, (C) write after and leave uncommitted
   between tasks. (A) loses the checkpoint_rev in the file content
   because the rev is the rev being created — self-referential. (B)
   doubles commit count. (C) matches the existing pattern: `sync_to_markdown`
   already writes `specs/task-list.md` after the checkpoint, leaves it
   uncommitted, and lets the next task's checkpoint absorb it. Going
   with (C) keeps the file content == DB content (rev included), and
   the trailing-dirty-workspace invariant is already accepted between
   tasks while the pipeline lease is held.

2. **Slug truncation that lands on a separator.** First pass returned
   `slug[:64]` and missed the case where character 64 is a `-`, leaving
   a trailing dash that looks ugly in filenames. Fixed by re-stripping
   after truncation: `slug[:_SLUG_MAX_LEN].rstrip("-") or "task"`. The
   trailing fallback is needed because `rstrip` could conceivably empty
   the string for inputs that are all dashes after slugification (which
   the earlier strip already handles, but defense in depth is cheap).

3. **OSError simulation without monkeypatching.** Pre-creating
   `diary/` as a regular file (not a directory) produces a hostile FS
   state where both `mkdir(exist_ok=True)` and `write_text` fail with
   real `OSError`. Avoids monkeypatching `Path` internals and exercises
   the actual error path.

4. **Mirroring DB content.** Earlier I sketched a separate
   `_build_filesystem_diary` builder that omitted the rev line, on the
   theory that the rev was unknown at write time. Once I committed to
   the post-checkpoint placement that became unnecessary — the file is
   written after `_maybe_create_checkpoint` so the rev IS known. One
   builder, two consumers.

### Postcondition verification

- [PASS] file_exists: `src/build_your_room/stages/impl_task.py`
  contains `_slugify` and `_maybe_write_diary_file`
- [PASS] tests_pass: 16 new tests, 1336 total (was 1320), 0 warnings
- [PASS] lint_clean: ruff check src/ tests/ → All checks passed
- [PASS] type_check: mypy src/ → Success: no issues found in 40 files
- [PASS] server boots: uvicorn → curl / → 200

### Open questions

- Should the diary file be committed as part of the SAME checkpoint as
  the agent's work, by writing pre-checkpoint and dropping the rev
  line from file content? Current placement keeps file == DB but means
  the file rides along in the next commit (or in code_review's). Both
  are defensible — current placement matches `sync_to_markdown`'s
  precedent.

- Should we also write a `diary/index.md` or `diary/README.md` that
  lists tasks in completion order? Out of scope for this task; would be
  a small follow-up if agents ask for it.
