# Task 21 — Pipeline Detail Page

## What was built

Full pipeline detail page at `GET /pipelines/{id}` with:

1. **Route** (`routes/pipelines.py`): `_fetch_pipeline_detail` does a single-connection query batch — pipeline+repo+def JOIN, all stages with sessions attached, recent logs (last 50), full HTN task tree with deps, escalations. Helper functions: `_build_task_tree` (flat→nested), `_parse_stage_graph` (JSON→viz data). Also added `GET /pipelines/{id}/logs` as an HTMX partial for live polling.

2. **Template** (`pipeline_detail.html`): Six sections — header/meta, lease+workspace health, stage graph (nodes with active highlighting + visit counts, edges with guards), stage execution tabs (with sessions, artifacts, revisions, escalation reasons), HTN task tree (recursive via partial, progress bar), live logs (HTMX 5s polling), clone management (copy path, cleanup).

3. **Partials**: `htn_task_node.html` (recursive with `{% include %}` + `{% with %}` for nesting, details/summary for compound tasks, decision resolve button), `pipeline_logs.html` (log entry list).

## Key decisions

- **Task tree building**: Flat DB query + Python-side nesting rather than recursive SQL CTEs. Simpler, sufficient for the expected tree sizes.
- **HTMX polling vs SSE**: Used HTMX polling (`hx-get` every 5s) for logs rather than SSE. Simpler and the LogBuffer's subscribe method is better suited for SSE but the HTMX approach is adequate for the dashboard and avoids connection-holding complexity.
- **Status class mapping**: Used `task-{status}` with underscores preserved (e.g., `task-not_ready`) to match DB status values directly, caught by property-based test.
- **Stage visit counts**: Computed from pipeline_stages rows grouped by stage_key, annotated on both graph nodes and stage tabs.

## Learnings

- Jinja2's recursive template inclusion via `{% with task=child %}{% include "partials/..." %}{% endwith %}` works well for tree rendering.
- Property-based tests immediately caught the CSS class naming mismatch — the status `not_ready` would have produced `task-not-ready` instead of `task-not_ready`. Hypothesis found this on first run.
- The `IN (...)` pattern with dynamic placeholders continues to work well for batched session/dep queries — just need to guard against empty lists.
- Total cost: summed from enriched stages' sessions rather than a separate DB query, avoiding an extra round trip.
- Test count: 45 new tests covering all spec requirements. 691 total (640 + 45 new + 6 from modified escalation tests).
