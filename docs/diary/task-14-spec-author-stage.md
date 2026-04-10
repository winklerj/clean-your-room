# Task 14: SpecAuthorStage + Review Loop Integration
## Session | Complexity: medium | Tests: 25 new (461 total)

### What I did
- Created `src/build_your_room/stages/spec_author.py` with `run_spec_author_stage()` async function
- Stage runner builds `SessionConfig` from pipeline DB row, stage node, and `WorkspaceSandbox`
- Resolves prompt template by name from `prompts` table, falls back to the name string itself
- Creates `agent_sessions` DB row to track the primary session lifecycle
- Starts a primary agent session, sends the authoring prompt, saves the output artifact to `{pipelines_dir}/{pipeline_id}/artifacts/spec.md`
- When `node.review` is configured, creates a `ContextMonitor` + `StageContext` and delegates to `run_review_loop()`
- On review escalation, creates an escalation row and returns `"escalated"`; on approval returns `"approved"`
- Updates `pipeline_stages.output_artifact` and `pipeline_stages.status` in the DB
- Wired `spec_author` dispatch into `orchestrator.py._run_stage()` replacing the stub path
- Two cancellation gates: before session start and after authoring before review
- Exception handler marks session as `"failed"` and re-raises; `finally` block always closes the session

### Learnings
- The orchestrator's `stage_id` is `Any | None` from the DB query result pattern — mypy caught the type mismatch when passing it to `run_spec_author_stage(stage_id: int)`. Fixed with a `stage_id is not None` guard in the dispatch.
- Hypothesis property tests that use `tmp_path` fixture fail with a health check because the fixture is function-scoped but not reset between `@given` examples. The fix: use `tempfile.TemporaryDirectory()` context manager inside the test body instead of relying on the fixture.
- Hypothesis `st.text()` can generate `\r` (carriage return) which Python's `write_text()` normalizes to `\n` on macOS, breaking roundtrip assertions. Filter with `blacklist_characters="\r"`.
- The stage runner pattern is clean: a module-level async function that receives all dependencies as explicit keyword arguments. This makes it testable (pass mocked pool/adapters) and keeps orchestrator.py thin (single dispatch line per stage type).
- When the review adapter is missing (e.g., codex not configured), the stage gracefully skips the review and returns approved. This prevents hard failures during development when only one adapter is available.
- The `_resolve_prompt()` fallback lets stages work even without seeded prompts — useful for custom pipelines where the prompt name IS the prompt body.

### Architecture decisions
- `spec_author.py` follows the same directory convention as `review_loop.py` — a module in `stages/`
- Each stage runner owns its own DB helper functions (`_create_session_row`, `_complete_session`, etc.) rather than sharing a base class. This keeps the dependency graph flat and each module independently testable.
- The stage runner does NOT call `_update_stage_status` when there's no review — the orchestrator's existing `_run_stage()` already marks the stage completed after the runner returns. The runner only updates stage status when a review loop runs (because approval/failure status carries extra info like escalation_reason).
- Escalation creation is the stage runner's responsibility, not the orchestrator's, because the stage has the context (round count, artifact path, warnings) needed for a useful escalation record.

### Postcondition verification
- [PASS] ruff check: All checks passed
- [PASS] mypy: Success, no issues found
- [PASS] pytest: 461 passed (25 new)
