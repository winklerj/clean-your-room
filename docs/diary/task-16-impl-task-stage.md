# Task 16: ImplTaskStage with Atomic HTN Task Claims, Context Rotation, and Postcondition Verification
## Session | Complexity: medium-high | Tests: 26 new (524 total)

### What I did
- Created `src/build_your_room/stages/impl_task.py` with `run_impl_task_stage()` async function
- Stage runner implements the core implementation loop: claim next ready HTN task -> create agent session -> send task prompt -> check context after each turn -> verify postconditions -> complete task -> repeat until no tasks remain
- Atomic HTN task claims via `HTNPlanner.claim_next_ready_task()` with `FOR UPDATE SKIP LOCKED` — safe for concurrent Agent Teams workers
- Context rotation with claim preservation: when `ContextMonitor.check()` returns ROTATE, the stage ends the current session gracefully, persists `resume_state_json`, creates a replacement session, and calls `planner.reassign_claim()` to update `assigned_session_id` — the task stays `in_progress` throughout
- Postcondition verification loop: after the agent completes a turn, `planner.verify_postconditions()` runs the condition checks. On failure, a follow-up prompt is sent with failure details. Max 3 retry rounds before the task is failed and escalated
- Stage-complete vs escalation logic: when `claim_next_ready_task()` returns None, checks `get_progress_summary()` to distinguish "all done" (stage_complete) from "tasks blocked/failed" (escalate)
- Max iterations guard: prevents infinite loops when tasks keep getting claimed
- Cancellation at two boundaries: before claim and after turn. Cancellation releases the claim and marks session cancelled
- `_build_task_prompt()` includes task name, description, and postconditions as bullet items
- `_build_resume_prompt()` adds continuation context for rotated sessions
- Wired `impl_task` dispatch into `orchestrator.py._run_stage()`
- 26 tests: 14 integration (single task, multi-task, postcondition retry/failure, context rotation with resume state, no-tasks scenarios, cancellation), 4 unit (prompt construction), 4 property-based (prompt invariants, postcondition bullet counts, context monitor determinism), 4 edge cases (missing adapter, max iterations, session exceptions)

### Learnings
- The mock planner pattern (`_make_mock_planner`) with `side_effect` lists for `claim_next_ready_task` is effective for simulating the task-claim-then-None termination pattern. Using `[task1, task2, ..., None]` as the side_effect naturally models the "no more tasks" boundary.
- Context rotation testing requires two mock sessions with different `start_session` side effects. The first returns high context usage (70% > 60% threshold), triggering rotation. The second returns low usage, allowing normal completion. The key assertion is that `reassign_claim` was called (preserving the claim) but `release_claim` was NOT called.
- The `resume_state_json` persistence is verifiable through DB queries after rotation — the first session row should have status `context_limit` and a non-null `resume_state_json` column.
- Postcondition retry testing uses `side_effect` lists on `planner.verify_postconditions`: `[[fail_result], [pass_result]]` models "fail once, then pass". The test verifies `send_turn` was called twice (initial + retry).
- When no tasks are claimed but some are blocked/failed, the stage must escalate rather than return stage_complete. The `get_progress_summary()` dict keys (`failed`, `blocked`, `in_progress`, `not_ready`) drive this decision.
- The `_execute_task_with_rotation` helper handles the complex inner loop (session lifecycle + context rotation + postcondition retries) as a single function returning bool. This keeps `run_impl_task_stage` focused on the outer claim loop.
- Unlike spec_author and impl_plan which each handle one artifact, impl_task manages a stateful claim lifecycle across potentially many sessions. The `finally: await session.close()` pattern in the inner function ensures cleanup even on exceptions.
