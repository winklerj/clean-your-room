# Task 40: Add pause/kill pipeline buttons to escalation cards

## What I did
- Added "Pause Pipeline" and "Kill Pipeline" action buttons to the escalation card template
- Pause button appears only when the associated pipeline is `running`
- Kill button appears when the pipeline is in any active state: `running`, `paused`, `needs_attention`, or `cancel_requested`
- Buttons POST to the existing `/pipelines/{id}/pause` and `/pipelines/{id}/kill` handlers
- Kill button has a JS confirm dialog matching the pipeline detail page pattern
- Added CSS for the `.escalation-pipeline-actions` container with flex layout
- Wrote 9 new tests: 7 deterministic + 2 property-based (Hypothesis)

## Learnings
- Hypothesis property-based tests sharing DB state across examples is a recurring pattern. When testing for the *absence* of an element in rendered HTML, generic CSS class checks (`btn-action-kill not in text`) fail because leftover data from prior examples can contain that class. Fix: check for the *specific pipeline's URL* (e.g., `/pipelines/{pid}/kill`) instead of generic button classes.
- The escalation data fetch already JOINs `pipelines` and exposes `pipeline_status` on each row — no backend changes needed, only template changes.
- Reused the existing `btn-action-pause` and `btn-action-kill` CSS classes from pipeline_detail.html rather than creating new styles.

## Postcondition verification
- [PASS] ruff check: all checks passed
- [PASS] mypy: 0 issues in 39 source files
- [PASS] pytest: 1122 passed (2 pre-existing flaky hypothesis failures unrelated to changes)
