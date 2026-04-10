# Task 49: Decision task pink coloring + CSS class hyphenation fix

## What I did
- Added pink (#e91e63) border-left coloring for decision-type HTN tasks per spec line 958 ("pink=needs human")
- Decision tasks with status != completed now get `task-decision` CSS class override in `_build_task_tree()`
- Completed decision tasks revert to standard green (`task-completed`) coloring
- Fixed CSS class name mismatch: `_TASK_STATUS_CLASS` was generating `task-in_progress` and `task-not_ready` (underscores) but CSS selectors expected `task-in-progress` and `task-not-ready` (hyphens)

## Learnings
- **Silent CSS class mismatches**: The status-to-class mapping used underscores (`task-in_progress`) while the CSS used hyphens (`task-in-progress`). These are different class names in CSS — the styling for in-progress (blue) and not-ready (light gray) tasks was silently broken. No existing test caught this because they only checked for `task-completed` and `task-failed` (which have no underscores).
- **Type vs status coloring**: Decision tasks are a *task_type*, not a *status*. The spec's "pink=needs human" maps to the decision type, not a status value. The override must be conditional on status — completed decisions should look like any other completed task (green).
- **PBT caught the underscore issue**: The existing property-based test `test_htn_task_status_renders_with_class` expected `task-{status}` which matched the old underscore pattern. Updating it to use `task-{status.replace('_', '-')}` was needed to align with the CSS convention.

## Postcondition verification
- [PASS] All 1186 tests pass
- [PASS] ruff lint clean
- [PASS] mypy type check clean
- [PASS] CSS class names match between Python and CSS selectors
- [PASS] Decision tasks render with pink border on both pipeline detail and tasks page
- [PASS] Completed decisions render with green, not pink
