# Task: Add HTN task tree standalone page
## Session: 35 | Complexity: medium | Revision: pending

### What I did
- Added GET /pipelines/{id}/tasks HTML route for a standalone HTN task tree view
- Created htn_tasks.html template with progress bar, status/type filter chips, and back-link
- Added "View full tree" link in pipeline detail page HTN section header
- Added CSS for section links, status/type filter chips, and back-link
- Supports query-param filters: ?status_filter=completed&type_filter=primitive
- Filter preserves ancestor chain so tree structure remains intact

### Learnings
- The spec explicitly lists GET /pipelines/{id}/tasks as a separate HTML route from the pipeline detail page; the existing pipeline detail already had an embedded task tree but no standalone page
- Ancestor preservation for filtered trees requires walking the parent_task_id chain to include compound parents even when they don't match the filter criteria
- The _build_task_tree helper was already available and could be reused for both the pipeline detail embedded view and the standalone page
- Filter chips provide a clickable shortcut for each status/type, with active highlighting and a clear-filters link

### Postcondition verification
- [PASS] ruff check src/ tests/ — all clean
- [PASS] mypy src/ --ignore-missing-imports — 0 errors
- [PASS] pytest tests/test_routes_htn_tasks.py — 15/15 pass
- [PASS] pytest tests/ — 1009 passed (2 pre-existing property-based test flakes)

### Open Questions
- None — this was a straightforward missing route from the spec
