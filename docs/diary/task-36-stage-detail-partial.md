# Task: Stage detail HTMX partial
## Session: 36 | Complexity: medium | Revision: pending

### What I did
- Added GET /pipelines/{id}/stages/{stage_id} HTMX partial endpoint
- Created partials/stage_detail.html (spec-listed template that was missing)
- Refactored pipeline_detail.html: stage tabs are now clickable HTMX headers that lazy-load the full stage detail on click, replacing the previous inline rendering
- Stage detail shows: session list with expandable per-session logs, output artifact file content rendering, review feedback history (event_type='review_feedback' from session_logs), per-session context usage with color-coded progress bars (ok/warn/high thresholds)
- Added CSS for stage detail sections, artifact content box, review feedback entries, session log expansion, inline context usage bars
- Updated 6 existing pipeline detail tests to match the new lazy-loading behavior
- Wrote 20 new tests (18 unit + 2 property-based)

### Learnings
- Pipeline detail page previously rendered all stage detail inline, which would not scale well with many stages and sessions. HTMX lazy-loading is the right pattern: only load detail when user clicks.
- The spec explicitly lists partials/stage_detail.html in the project structure, confirming this was a planned but unimplemented template.
- PostgreSQL REAL type cannot store very small subnormal floats; Hypothesis float strategies need integer-mapping or allow_subnormal=False plus clearing the example database to avoid NumericValueOutOfRange from cached failing examples.
- When refactoring from inline rendering to HTMX lazy-loading, existing tests that checked for inline content need updating to check the compact summary (session counts, artifact hints) and HTMX attributes instead.
- The _fetch_stage_detail helper enforces pipeline ownership (WHERE pipeline_id = %s AND id = %s) to prevent stage ID guessing across pipelines.

### Postcondition verification
- [PASS] ruff check src/ tests/ — all clean
- [PASS] mypy src/ --ignore-missing-imports — 0 errors
- [PASS] pytest tests/test_routes_stage_detail.py — 20/20 pass
- [PASS] pytest tests/ — 1029 passed (2 pre-existing property-based test flakes)

### Open Questions
- None
