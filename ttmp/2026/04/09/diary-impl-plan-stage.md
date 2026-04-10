# Diary: ImplPlanStage — implementation planning with HTN population

## Task
Phase 3, Task 15: ImplPlanStage + review loop integration + HTN task graph population from structured output.

## What I did
- Completed `stages/impl_plan.py` with the full ImplPlanStage runner
- Added `parse_htn_tasks()` — public validator for structured HTN output with defensive None/type/key checks
- Added `HTN_TASK_OUTPUT_SCHEMA` — JSON schema sent to the agent for structured task decomposition output (plan_markdown + tasks array)
- Added `_parse_plan_output()` — dual-path parser: prefers structured output, falls back to raw text extraction
- Added `_try_extract_tasks_json()` — fallback parser for fenced JSON blocks and inline JSON in raw agent output
- Added `_load_spec_artifact()` — loads spec.md from previous stage as planning context
- Integrated `HTNPlanner.populate_from_structured_output()` after plan approval for DB task graph population
- Added `sync_to_markdown()` call after HTN population for agent readability
- Added injectable `htn_planner` parameter for testability
- Wired `impl_plan` stage type into orchestrator `_run_stage()` dispatch
- Wrote 37 tests: 11 unit (parse_htn_tasks, _parse_plan_output, _try_extract_tasks_json, artifact path, spec loading), 22 integration (happy path, HTN population with deps/readiness, review loop, escalation, cancellation, missing adapters, session lifecycle), 4 property-based (structured output roundtrip, artifact write roundtrip, fenced JSON extraction, schema validation)

## Learnings
- The prior session had already created skeletal `impl_plan.py` and `test_impl_plan.py` files with a solid foundation — the implementation was close to complete. Key additions needed: `parse_htn_tasks` public function, `htn_planner` injectable parameter, and `sync_to_markdown` after HTN population.
- The `_parse_plan_output` dual-path strategy (structured output preferred, raw text fallback with fenced JSON extraction) is robust against agents that don't reliably produce structured output. The fallback parser handles both `{"tasks": [...]}` objects and bare `[...]` arrays.
- HTN population happens AFTER review approval (correct flow) — if review escalates, the early return skips population. If no review is configured, population runs immediately.
- The pool_with_stage fixture for impl_plan tests uses `tmp_path / "clone" / "specs"` for clone_path so `sync_to_markdown` can write the task-list.md file to disk without errors.
- Review loop tests use `unittest.mock.patch` on `build_your_room.stages.impl_plan.run_review_loop` to return a controlled `ReviewLoopOutcome`, avoiding the need to set up full review session mocking.

## Postcondition verification
- [PASS] `uv run ruff check src/ tests/` — all checks passed
- [PASS] `uv run mypy src/ --ignore-missing-imports` — 0 errors
- [PASS] `uv run pytest tests/ -v` — 498/498 tests pass (37 new)
