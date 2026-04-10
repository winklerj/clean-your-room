# Task 9: HTNPlanner — task graph CRUD, atomic claims, readiness propagation

## What was done

Implemented `src/build_your_room/htn_planner.py` — a pure data-layer component that manages the full HTN task graph lifecycle without direct agent interaction:

1. **`populate_from_structured_output()`**: Three-pass population from agent-produced task JSON. First pass inserts all tasks (resolving parent names to IDs), second pass creates `htn_task_deps` edges, third pass computes initial readiness.

2. **`claim_next_ready_task()`**: Atomic claim using PostgreSQL `WITH candidate AS (SELECT ... FOR UPDATE SKIP LOCKED) UPDATE ... RETURNING`. Prevents concurrent Agent Teams workers from racing on the same task. Returns the claimed HtnTask or None.

3. **`release_claim()` / `reassign_claim()`**: Claim lifecycle for context rotation — reassign keeps the claim active on a new session (spec's `ClaimedTaskResumedOrReleased` invariant), release returns the task to 'ready'.

4. **`verify_postconditions()`**: Delegates to the existing `verify_condition()` dispatcher from `command_registry.py`. Pre-loads completed task names for `task_completed` condition type resolution via synchronous callback.

5. **`complete_task()` + readiness propagation**: Marks task completed, then checks all hard dependents — those with ALL hard deps met transition from `not_ready` → `ready`. Also auto-completes compound parents when all children are done.

6. **`fail_task()`**: Marks task failed and blocks all hard dependents.

7. **`create_decision_escalation()` / `resolve_decision()`**: Decision-type tasks get blocked with an open escalation; resolution completes the task and propagates readiness.

8. **`get_task_tree()` / `get_task_deps()` / `get_progress_summary()`**: Dashboard query methods.

9. **`sync_to_markdown()`**: Writes a human/agent-readable task list to `specs/task-list.md` with status icons, hierarchy, and dependency info.

35 tests: 29 unit tests covering all methods + 6 property-based tests (Hypothesis) verifying populate count, summary sum, and compound readiness invariants.

## Learnings

- PostgreSQL `FOR UPDATE SKIP LOCKED` in a CTE works well for atomic claiming — the CTE selects+locks a candidate, then the outer UPDATE claims it. If another transaction holds the lock, the row is skipped and the next candidate is tried.

- Readiness propagation requires careful ordering: you must check ALL hard deps, not just the one that just completed. The `_recompute_readiness()` helper re-checks everything from scratch, which is safe for the expected task graph sizes.

- The `RETURNING` clause with a CTE + UPDATE requires table-qualified column names (`t.id`, `t.name`, etc.) since the JOIN between `htn_tasks AS t` and `candidate` introduces ambiguity. Fixed by adding `_TASK_COLUMNS_QUALIFIED` constant with `t.`-prefixed column names for the claim query.

- mypy's `dict_row` type inference for psycopg returns `tuple` by default. Bare `# type: ignore` on row access lines is the pragmatic fix — the orchestrator uses `# type: ignore[assignment]` on fetchone calls instead. Both work; consistency matters more than which approach.

- Property-based tests with Hypothesis + pytest-postgresql need unique names per test run to avoid UNIQUE constraint violations. Using `uuid.uuid4().hex[:8]` suffixes in the seed helpers works well. Also need `suppress_health_check=[HealthCheck.function_scoped_fixture]` since the `initialized_db` fixture is function-scoped but each hypothesis input gets its own unique pipeline.

## Test count

Before: 271 tests
After: 306 tests (+35)
