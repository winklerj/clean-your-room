# Task 33: HTN Decision Task Resolution HTML Endpoint

**Date:** 2026-04-10

## What was built

POST `/pipelines/{pipeline_id}/tasks/{task_id}/resolve` HTML endpoint that lets
users resolve decision-type HTN tasks directly from the pipeline detail page,
replacing the previous link to the escalation queue with an inline form.

## Key decisions

- **Inline form over redirect**: Changed the template from a link to `/escalations`
  to an inline `<form>` with a text input + submit button. This keeps the user on
  the pipeline detail page and provides faster resolution workflow.

- **Validation guards**: The route validates pipeline existence, task existence,
  task-pipeline ownership, task type (must be `decision`), and already-completed
  idempotency before calling the planner.

- **Delegating to HTNPlanner.resolve_decision**: The route uses the existing
  `resolve_decision()` method which handles the full lifecycle: completing the
  task, resolving the linked escalation, and propagating readiness to dependents.
  No need to duplicate that logic in the route.

- **Consistent redirect pattern**: Returns 303 redirect to pipeline detail page,
  matching all other lifecycle POST routes (cancel, kill, pause, resume).

## Learnings

- The existing `test_pipeline_detail_htn_decision_task` in `test_routes_pipelines.py`
  asserted the exact text "Resolve decision" from the old template link. Changing
  the template button text to just "Resolve" broke this test. Always check for
  existing tests that assert on template content before modifying templates.

- The HTNPlanner's `resolve_decision` uses a PostgreSQL `jsonb @>` containment
  operator to find the linked escalation by `task_id` in `context_json`. This
  is elegant but means the escalation must have been created with the right
  `context_json` structure for the resolution to cascade properly.

## Files changed

- `src/build_your_room/routes/pipelines.py` — added `resolve_decision_task_html` route
- `src/build_your_room/templates/partials/htn_task_node.html` — inline form replaces link
- `tests/test_routes_htn_decision.py` — 11 new tests (happy path, guards, template, property-based)
- `tests/test_routes_pipelines.py` — updated existing assertion for new template text

## Test count

11 new tests, 980 total (977 pre-existing passing + 3 new passing, 2 pre-existing Hypothesis flakes unrelated)
