# Tasks — DASH-001: Dashboard Health Cards

## Phase 1: Backend — Enrich Dashboard Route
- [x] Add aggregate job stats query (GROUP BY status)
- [x] Extend repo query with iteration info, last job ID, last completed timestamp
- [x] Pass job_stats, total_repos to template context

## Phase 2: Frontend — Template and CSS
- [x] Add 4 summary stat cards (Total Repos, Running, Completed, Failed)
- [x] Add Progress column with iteration bar
- [x] Add Specs column with freshness timestamp
- [x] Add "Run again" button (POST to existing restart endpoint)
- [x] Add CSS for stat cards, progress bars, freshness, run-again button

## Phase 3: Tests
- [x] test_dashboard_empty — empty state renders
- [x] test_dashboard_stats_cards — aggregate counts in cards
- [x] test_dashboard_repo_with_completed_job — status badge + freshness
- [x] test_dashboard_repo_no_jobs — no-jobs badge, no specs yet
- [x] test_dashboard_run_again_button — button present with correct URL

## Verification
- [x] `uv run ruff check src/ tests/` passes
- [x] `uv run pytest tests/test_routes_dashboard.py -v` — 5/5 pass
- [x] `uv run mypy src/clean_room/routes/dashboard.py --ignore-missing-imports` — 0 errors in dashboard.py
- [ ] Pre-existing: 23 mypy errors in db.py/jobs.py/repos.py (out of scope)
- [ ] Pre-existing: test_runner_closes_buffer_on_completion failure (out of scope)
