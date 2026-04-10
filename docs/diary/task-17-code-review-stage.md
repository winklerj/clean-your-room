# Task 17: CodeReviewStage with Full-Head Diff Review and Bug-Fix Loop
## Session | Complexity: medium | Tests: 32 new (556 total)

### What I did
- Refined `src/build_your_room/stages/code_review.py` with `run_code_review_stage()` async function
- Stage runner implements the review/fix cycle: capture full diff from `review_base_rev` to `head_rev` → send to review agent → parse structured approval → if issues found, run fix agent → re-review → repeat
- Enforces the **ReviewCoversHead** invariant: diff always spans the full pipeline baseline to current HEAD, not just uncommitted changes
- Diff materialization: full diff is captured via `git diff review_base_rev...head_rev` and saved to `artifacts/review/full_diff.patch` for auditability
- Review uses `REVIEW_OUTPUT_SCHEMA` from review_loop.py for structured JSON output — reuses the same `parse_review_result` and `should_approve` decision gate
- Bug-fix loop: when review finds issues, a new fix session receives the issues as JSON with a dedicated `bug_fix_default` prompt. After fixing, the diff is re-captured and another review round runs
- `_build_fix_prompt()` formats issues with file:line locations, severity levels, and feedback markdown
- Added `on_max_rounds` field to `StageNode` (with JSON parsing) for configurable escalation vs proceed-with-warnings behavior
- Orchestrator dispatch wired for `code_review` stage type
- 32 tests: 5 happy-path approval (first-round approval, session rows, diff artifact, DB artifact reference, session close), 3 bug-fix loop (rejected-then-approved, session rows for fix cycles, log output), 3 escalation (max rounds, proceed_with_warnings, unparseable output), 9 edge cases (empty diff, whitespace-only diff, missing review/fix adapters, cancel before/between rounds, fix agent failure propagation, null session_id, null head_rev), 3 prompt resolution, 4 property-based (deterministic decisions, approved-implies-low-severity, diff artifact roundtrip)

### Learnings
- When both review and fix agents use the same adapter key (e.g. both "codex"), the test mock's `start_session.side_effect` list must interleave review and fix sessions in call order: [review1, fix1, review2, fix2, review3]. This models the real behavior where one adapter pool serves both roles.
- The `_capture_full_diff` function uses `asyncio.subprocess.PIPE` (not `subprocess.PIPE`) — a subtle but critical difference for async subprocess execution. Using `subprocess.PIPE` with `asyncio.create_subprocess_exec` is a common mistake that can cause hangs or errors.
- Empty/whitespace-only diffs should short-circuit to approval without starting any review sessions. This prevents unnecessary agent invocations when there are genuinely no changes to review (e.g., pipeline re-entry after validation).
- The `on_max_rounds` config is properly a StageNode-level field (not ReviewConfig-level) for code_review stages, since code_review doesn't use the review sub-config pattern used by spec_author/impl_plan.
- Fix session failures (exceptions) should propagate up rather than being silently caught — the orchestrator's error handling will mark the pipeline as failed. The `_run_fix_turn` helper marks the session as "failed" in the DB before re-raising.
- When `head_rev` is NULL in the pipeline (no commits from implementation yet), the stage falls back to using `review_base_rev` as both ends of the diff range, producing an empty diff and approving immediately.
