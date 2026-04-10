# Task 15: ImplPlanStage + Review Loop Integration + HTN Task Graph Population
## Session | Complexity: medium | Tests: 37 new (498 total)

### What I did
- Created `src/build_your_room/stages/impl_plan.py` with `run_impl_plan_stage()` async function
- Stage runner builds on the SpecAuthorStage pattern but adds: (1) loading the spec artifact from the previous stage as prompt context, (2) requesting structured HTN output via `HTN_TASK_OUTPUT_SCHEMA`, (3) parsing the structured output with `_parse_plan_output()` + `parse_htn_tasks()`, (4) populating the HTN task graph via `HTNPlanner.populate_from_structured_output()`, (5) syncing the task list to markdown via `planner.sync_to_markdown()` for agent readability
- `parse_htn_tasks()` validates structured output: requires `tasks` key as a list where each item has `name` and `description`
- `_try_extract_tasks_json()` provides fallback parsing for raw text output: searches for fenced ```json blocks, then inline JSON objects/arrays with a `tasks` key
- `HTN_TASK_OUTPUT_SCHEMA` constant defines the JSON schema sent to the agent for structured task decomposition
- Wired `impl_plan` dispatch into `orchestrator.py._run_stage()` following the `spec_author` pattern
- Injectable `htn_planner` parameter for test isolation
- 37 tests covering: artifact path (unit + property-based), spec artifact loading, plan output parsing (structured + fallback), `parse_htn_tasks` validation (6 cases + 1 property), HTN task population with dependency edges, `sync_to_markdown` integration, cancellation, missing adapters, session failure handling, review loop integration (approved + escalated), spec context forwarding, and property-based roundtrip tests

### Learnings
- The `sync_to_markdown()` method on HTNPlanner writes to `{clone_path}/specs/task-list.md`. The test fixture needed to use `tmp_path` for the clone_path and create the `specs/` directory, otherwise filesystem writes failed silently or errored. This differs from the spec_author tests where clone_path was `/tmp/test-clone` (never written to).
- The `_parse_plan_output()` two-tier strategy (structured output preferred, raw text fallback) is important because different models and adapter configurations may or may not support structured output. Claude SDK supports `output_schema`, Codex JSON-RPC may not — the fallback ensures robustness.
- The linter auto-improved the implementation: added a standalone `parse_htn_tasks()` validator, made `htn_planner` injectable, and added `sync_to_markdown` after population. These are good patterns — injectable dependencies for testing and post-population sync for agent readability.
- HTN task dependencies are stored as separate `htn_task_deps` rows with `dep_type='hard'`. The planner resolves task names to DB IDs in a second pass after all tasks are inserted. Unknown dependency names are silently skipped — this is intentional since agents may reference non-existent tasks.
- Property-based tests for `_try_extract_tasks_json` caught that the fenced block regex needs `re.DOTALL` to match multi-line JSON. Without it, the `.*?` in the regex only matches within a single line.

### Architecture decisions
- `impl_plan.py` mirrors `spec_author.py` structure: same private DB helpers, same review loop integration, same escalation handling. This duplication is intentional per Task 14's decision — each stage runner owns its queries for now, with consolidation deferred until patterns emerge across all five stage runners.
- Spec artifact loading is a simple filesystem read from the artifacts directory rather than a DB query. This follows the convention that the spec artifact path is stored in `pipeline_stages.output_artifact`, but the content is on disk.
- The `HTN_TASK_OUTPUT_SCHEMA` is co-located with the stage runner rather than in `htn_planner.py` because it's agent-facing (defines what the agent should return), not DB-facing (how tasks are stored). The planner's `populate_from_structured_output` handles the DB mapping.
- HTN population happens AFTER the review loop returns approved. If the review escalates, no tasks are populated — the plan needs human intervention before tasks can be created.

### Postcondition verification
- [PASS] ruff check: All checks passed
- [PASS] mypy: Success, no issues found in 27 source files
- [PASS] pytest: 498 passed (37 new impl_plan tests)
