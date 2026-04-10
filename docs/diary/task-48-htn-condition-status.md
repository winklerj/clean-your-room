# Task 48: Show Precondition/Postcondition Status on HTN Task Nodes

## What I did
- Added `_parse_conditions()` helper to defensively parse preconditions_json/postconditions_json into display-friendly lists
- Extended `_build_task_tree()` to parse conditions for every task node
- Updated `partials/htn_task_node.html` with Preconditions/Postconditions sections showing condition type badges, descriptions, and status indicators
- Added CSS for condition display with status-appropriate colors (green=passed, red=failed, gray=pending)

## Design decisions
- **Status inference from task status**: Rather than running verifiers at display time (expensive), the template infers condition status from the task's own status: completed tasks show all conditions as passed (checkmark), failed tasks show postconditions as failed (X), in-progress tasks show preconditions as passed (they were checked at claim time) and postconditions as pending, ready/not_ready tasks show all as pending
- **Defensive JSON parsing**: `_parse_conditions()` handles empty strings, "null", malformed JSON, non-list values, and non-dict entries gracefully — always returns a list, never raises
- **Description fallback to type**: When a condition lacks a `description` field, the `type` value is used instead, so every condition always has displayable text
- **Conditions hidden when empty**: Empty `[]` conditions (the default) don't render Preconditions/Postconditions sections, avoiding visual noise on tasks without explicit conditions

## Learnings
- The `preconditions_json` and `postconditions_json` columns default to `'[]'` in the DB schema, so most tasks produce empty conditions lists
- Since `_build_task_tree()` is used by both the pipeline detail page and the standalone HTN tasks page, the conditions display appears consistently on both views via the shared `htn_task_node.html` partial
- Using monospace `condition-type-badge` for the condition type (file_exists, tests_pass, etc.) makes the type visually distinct from the description text and consistent with code-level terminology

## Postcondition verification
- [PASS] ruff check: all clean
- [PASS] mypy: 0 errors
- [PASS] pytest: 1174 tests, 0 warnings
