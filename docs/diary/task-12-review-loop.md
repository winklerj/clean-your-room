# Task 12: Review Loop Logic
## Session | Complexity: medium | Tests: 48 new (436 total)

### What I did
- Created `src/build_your_room/stages/` package with `__init__.py` and `review_loop.py`
- Implemented `ReviewResult` frozen dataclass and `parse_review_result()` with defensive parsing
- Implemented `should_approve()` (approved + severity <= low) and `should_always_feed_back()` (severity >= high)
- Implemented `run_review_loop()` — the core bounded feedback cycle between primary and review agents
- Implemented same-session continuation (send feedback directly on the existing session handle)
- Implemented new-session fallback (close old session, start replacement with self-contained prompt when context monitor triggers rotation)
- Implemented max-rounds boundary with `on_max_rounds` policy: `escalate` (default) or `proceed_with_warnings`
- High/critical severity always gets feedback even at max rounds, plus an extra review round to check if issues resolved
- Each review round creates a fresh review session (Codex) and properly closes it after parsing

### Learnings
- The decision gate has a subtle interaction: `approved=True` but `max_severity=high` should NOT count as approved — the severity gate overrides the approved flag. This prevents a confused reviewer from inadvertently passing a document with serious issues.
- Context rotation in the review loop is simpler than in the impl_task loop because there are no HTN task claims to preserve. The monitor just checks usage, and if rotating, the new session gets a self-contained prompt with the artifact reference and feedback markdown.
- The `_feed_back()` helper gracefully falls back to same-session if no `primary_adapter` is provided — this handles the case where rotation can't happen (e.g., adapter not configured or after server restart where same-session is not resumable anyway).
- The `ContextMonitor.parse_claude_usage()` returns `None` when max_tokens is 0, so the `_feed_back` codepath correctly handles this by skipping the context check entirely and defaulting to same-session continuation.
- Review sessions are ephemeral (one per round) while the primary session persists across rounds. This asymmetry is by design — the reviewer gets a clean context each round, while the primary accumulates context from successive revisions.
- Property-based tests caught a key invariant: `should_always_feed_back(r)` and `should_approve(r)` are always disjoint — no input can satisfy both. This is a consequence of the severity ordering but is non-obvious and worth verifying generatively.
- The `@st.composite` strategy `valid_structured_approvals` generates well-formed review output dicts with all required fields, making it easy to fuzz the decision gate logic across the full input space.

### Architecture decisions
- `review_loop.py` lives in `stages/` rather than `adapters/` — it orchestrates two adapters, so it belongs at the stage layer
- `REVIEW_OUTPUT_SCHEMA` is exported as a constant so stage runners (SpecAuthorStage, ImplPlanStage) can reference it
- The loop signature takes both adapter factories and session configs as parameters, making it testable with mocks and reusable across spec-author and impl-plan stages

### Postcondition verification
- [PASS] ruff check: All checks passed
- [PASS] mypy: Success, no issues found
- [PASS] pytest: 436 tests passed (48 new: 40 unit + 8 property-based)
