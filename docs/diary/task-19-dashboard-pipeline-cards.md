# Task 19: Dashboard template with pipeline cards grid + HTN progress indicators
## Session: 1 | Complexity: medium

### What I did
- Rewrote `routes/dashboard.py` with `_fetch_dashboard_data()` that queries pipelines (joined with repos), current stage info, HTN progress summaries (primitive tasks only per spec), context usage from active sessions, accumulated cost per pipeline, and open escalation count — all in a single DB connection
- Created `templates/partials/pipeline_card.html` partial showing: pipeline def name + repo name, current stage with iteration progress, HTN task progress bar with completed/active/failed counts, context usage bar (color-coded by threshold), lease heartbeat indicator, cost accumulator, and HTMX-powered cleanup button for terminal states
- Rewrote `templates/dashboard.html` with escalation banner, 5-column stat cards (total/running/needs_attention/completed/failed), pipeline cards grid, and repos table below
- Updated `templates/base.html` nav with Escalations link
- Extended `static/style.css` with pipeline card grid layout, status-specific left borders, HTN progress styling, context usage colors, escalation banner, and new status badge variants (needs_attention, paused, cancelled, killed)
- Wrote 20 tests covering: empty dashboard, pipeline card rendering, status counts, HTN progress bars, failed task counts, escalation banner show/hide, resolved escalation exclusion, stage iteration progress, context usage display, cost accumulation, cleanup button visibility, pipeline def names, multiple cards, repos table, nav links, archived repo hiding

### Learnings
- The linter/hook automatically reformatted the pipeline card template to use HTMX `hx-post` for cleanup instead of a form, renamed CSS classes to be more semantic (e.g., `progress-ctx-ok`/`progress-ctx-warn`/`progress-ctx-high` instead of reusing `progress-running`), and restructured the card layout for better information hierarchy — the pipeline def name became the card title with repo name underneath
- PostgreSQL's `DISTINCT ON` is ideal for getting the "latest stage per pipeline" in a single query without subqueries or window functions
- Tests that seed multiple DB rows with UNIQUE constraints need careful deduplication (e.g., unique `clone_path` for each pipeline, unique `name` for pipeline_defs)
- The existing test infrastructure (httpx AsyncClient + ASGITransport + pytest-postgresql) works seamlessly for testing the enriched dashboard queries with minimal boilerplate

### Postcondition verification
- [PASS] 619 tests pass (20 new dashboard tests, replacing 3 original)
- [PASS] ruff check clean
- [PASS] mypy clean
