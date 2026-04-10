# Diary: Clone Cleanup Buttons — per-pipeline and bulk

## Task
Phase 4, Task 24: Clone cleanup buttons (per-pipeline and bulk).

## What I did
- Added two POST endpoints to `routes/pipelines.py`:
  - `POST /pipelines/{id}/cleanup` — single pipeline clone cleanup with terminal-status guard (409 for non-terminal), directory deletion via `shutil.rmtree`, HTMX-aware response (card partial for HX-Request, 303 redirect otherwise)
  - `POST /pipelines/cleanup-completed` — bulk cleanup of all completed/cancelled/killed pipeline clones, redirects to dashboard
- Added `_fetch_pipeline_card_data()` helper for rendering a single pipeline card after HTMX cleanup
- Added `terminal_count` to dashboard data for conditional bulk button rendering
- Updated `dashboard.html` with "Clean all completed (N)" button in a flex header row, only visible when terminal pipelines exist
- Updated `pipeline_detail.html` cleanup button from HTMX to a standard form POST (detail page uses redirect, not swap)
- Added CSS for `.pipelines-header` layout and `.btn-cleanup-bulk` button styling
- 25 new tests covering: 404, 409 guard, directory deletion, idempotent cleanup, HTMX card response, redirect behavior, all 4 terminal statuses, 4 non-terminal statuses, bulk cleanup with selective deletion, bulk no-op, bulk skip missing, dashboard button visibility/count, detail page button visibility, 2 property-based tests

## Key decisions
- Used `shutil.rmtree` directly in the route handlers rather than delegating to CloneManager — the routes only need directory deletion, not the full CloneManager lifecycle (DB queries for finding completable pipelines). The route already validates status and resolves the path from the DB row.
- HTMX-aware single cleanup: checks `HX-Request` header to decide between returning a card partial (for dashboard card swap) or 303 redirect (for detail page). This avoids needing separate endpoints for the two UI contexts.
- Bulk cleanup uses standard form POST rather than HTMX — simpler, full page refresh shows updated state
- Detail page uses standard form POST instead of HTMX to get a proper redirect back to the same page

## Learnings
- The existing pipeline card template already had HTMX cleanup buttons wired with `hx-post`, `hx-target`, and `hx-confirm` from Task 21 — just needed the backend endpoint
- Testing HTMX behavior is straightforward with httpx: pass `headers={"HX-Request": "true"}` and assert on the HTML partial response vs redirect
- The `_fetch_pipeline_card_data` helper duplicates some logic from `_fetch_dashboard_data` enrichment, but extracting shared code would create coupling between dashboard and pipeline routes — acceptable duplication for two routes that serve different templates

## Test count
25 new tests (6 per-pipeline + 4 terminal parametrized + 4 non-terminal parametrized + 4 bulk + 3 dashboard button + 2 detail page + 2 property-based). 779 total.
