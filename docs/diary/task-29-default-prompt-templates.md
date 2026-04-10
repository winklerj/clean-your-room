# Task 29: Default prompt templates for all stage types
## Session: 1 | Complexity: medium | Phase: 5

### What I did
- Added 2 missing default prompts to `DEFAULT_PROMPTS` in db.py: `impl_task_default` (impl_task/claude) and `validation_default` (validation/claude)
- Enhanced 2 existing prompt bodies: `spec_author_default` (expanded from 1 line to comprehensive spec-writing instructions) and `impl_plan_review_default` (expanded from 1 line to plan review checklist)
- Created `specs/` directory with `default_prompts.json` (all 8 prompts as JSON) and `example_pipeline_def.json` (full-coding-pipeline from spec)
- Added `load_default_prompts_json()` utility in db.py for loading prompts from JSON
- Added `SPECS_DIR` constant for locating the specs directory
- Fixed `test_prompt_resolved_from_db` in test_validation.py — was using INSERT but `validation_default` is now seeded by `init_db`; changed to UPDATE
- Wrote 12 new tests in test_db.py (6 coverage, 4 JSON integrity, 2 property-based)

### Learnings
- The stage runners resolve prompts by name from the DB but DO NOT substitute `{{variable}}` template tokens — the body is used as-is as a base system prompt, with stage-specific code appending dynamic context (task details, spec content, diffs, etc.) at runtime
- Template variable extraction (`extract_template_variables`) only runs in the UI layer for display purposes
- Adding new seeded prompts can break tests that manually INSERT the same name — use UPDATE or ON CONFLICT when overriding seeded prompts in tests
- The `ON CONFLICT (name) DO NOTHING` pattern makes seeding idempotent but means updates to default prompt bodies won't propagate to existing databases — this is intentional (user edits are preserved)
- Two pre-existing flaky Hypothesis tests exist: `test_artifact_write_roundtrip` (impl_plan) and `test_parse_nodes_roundtrip` (pipeline_defs) — both involve whitespace edge cases

### Test count
- Before: 876 tests
- After: 888 tests (886 passing, 2 pre-existing flaky)
- New tests: 12 (6 prompt coverage + 4 JSON consistency + 2 property-based)

### Files changed
- `src/build_your_room/db.py` — added `json`/`Path` imports, `SPECS_DIR`, `impl_task_default`/`validation_default` prompts, enhanced `spec_author_default`/`impl_plan_review_default` bodies, `load_default_prompts_json()`
- `specs/default_prompts.json` — new: all 8 default prompts as JSON
- `specs/example_pipeline_def.json` — new: full-coding-pipeline definition from spec
- `tests/test_db.py` — 12 new tests, updated imports, updated idempotency count
- `tests/test_validation.py` — fixed INSERT→UPDATE for seeded `validation_default`
- `docs/plans/build-your-room-tasks.md` — marked task 29 complete
