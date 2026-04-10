# Task 7: ContextMonitor as a reusable hook

**Date:** 2026-04-09
**Status:** Complete

## What was built

`context_monitor.py` — a pure decision-maker hook that checks context-window
usage against a configurable threshold after every agent turn and returns
rotation instructions when the threshold is exceeded.

### Key types

| Type | Purpose |
|------|---------|
| `ContextUsage` | Frozen dataclass holding total/max tokens, usage %, and category breakdown |
| `StageContext` | Execution context needed to build a rotation plan (stage type, IDs, claim fields) |
| `RotationPlan` | Instructions for the adapter: resume_state dict + has_active_claim flag |
| `ContextCheckResult` | Full result: action (CONTINUE/ROTATE), usage, optional plan + warning |
| `ContextMonitor` | Stateful hook with threshold, check/warning counters, and parse helpers |

### Design decisions

1. **Pure logic, no side-effects.** The monitor does not touch the database,
   manage sessions, or interact with providers. Adapters own all side-effects;
   the monitor owns the policy. This makes it trivially testable and reusable
   across Claude and Codex adapters.

2. **Threshold boundary is inclusive.** Usage *at* the threshold yields
   CONTINUE, not ROTATE. This avoids unnecessary rotation at the boundary —
   rotation is only triggered when *exceeding* the threshold.

3. **Claim preservation for impl_task.** When the stage is `impl_task` and an
   HTN task claim is active, the rotation plan includes `task_id`,
   `claim_token`, and `prompt_context` in resume_state. Non-impl_task stages
   never produce `has_active_claim=True`, even if claim fields are accidentally
   populated.

4. **Static parsers for provider payloads.** `parse_claude_usage()` and
   `parse_codex_usage()` are static methods that return `None` for missing or
   nonsensical data (None input, zero/negative max_tokens). This prevents
   the monitor from operating on bogus data.

## Test coverage

38 new tests (229 total), including:

- **5 property-based tests** for threshold invariants: below-threshold always
  continues, above-threshold always rotates, result always contains usage,
  check_count equals calls, warning_count <= check_count.
- **Property tests for rotation plans**: core fields always present,
  artifact_path included iff present.
- **Property tests for parsers**: computed pct matches ratio, total equals
  input + output.
- **Unit tests**: boundary behavior (exact threshold), claim preservation for
  impl_task, non-numeric category exclusion, None/zero/negative guard values.
- **Integration tests**: parse-then-check pipeline for both Claude and Codex.

## Learnings

- Hypothesis `@st.composite` with `DrawFn` type annotation keeps strategies
  type-safe while remaining flexible (conditional draws for claim fields).
- Using `assume()` to filter generated usages above/below threshold is cleaner
  than trying to build constrained strategies that directly produce the right
  percentage range.
- The frozen-dataclass pattern continues to pay off: immutable value objects
  make property tests straightforward since there's no hidden mutation.
