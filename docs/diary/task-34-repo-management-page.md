# Task 34: Repo Management Page

**Date:** 2026-04-10

## What was built

GET `/repos` — a dedicated repo management page showing all repos with
pipeline history, status counts, latest pipeline info, and links to create
new pipelines. Also enriched the existing repo detail page with a pipeline
history table.

## Key decisions

- **Dedicated page over dashboard embed**: The spec lists `GET /repos` as a
  standalone route. The dashboard already shows a minimal repos table, but the
  dedicated page provides richer information: pipeline counts per status,
  latest pipeline with def name, and per-repo "New Pipeline" links.

- **Show/hide archived filter**: Rather than always showing archived repos
  or hiding them entirely, added a `?show_archived=true` query parameter
  with a toggle link. Default hides archived repos since they're usually
  not relevant.

- **Enriched repo detail**: The existing repo detail page was bare — just
  name, path, branch, and an archive button. Added a pipeline history table
  showing all pipelines that ran against the repo, matching the spec's
  requirement that repos show "all pipelines that have run against them".

- **Pipeline counts via GROUP BY**: Used a single GROUP BY query to get
  pipeline status counts per repo rather than N+1 queries. The latest
  pipeline per repo uses DISTINCT ON for efficiency.

- **Nav link**: Added "Repos" to the global nav bar between "Dashboard"
  and "Escalations" for easy discovery.

## Learnings

- PBT tests with `initialized_db` fixture need `suppress_health_check=
  [HealthCheck.function_scoped_fixture]` since the DB isn't reset between
  Hypothesis examples. Tests must use unique names (uuid-based) and avoid
  global assertions (like counting total cards) since state accumulates.

- The repo detail route needed its return type annotation updated from bare
  `fetchone()` to `dict[str, Any] | None` to match the psycopg `dict_row`
  pattern used throughout the codebase.

## Files changed

- `src/build_your_room/routes/repos.py` — added `GET /repos` route + `_fetch_repos_data`, enriched `repo_detail` with pipeline history
- `src/build_your_room/templates/repos.html` — new repo list template
- `src/build_your_room/templates/repo_detail.html` — rewritten with pipeline table, new pipeline link, improved layout
- `src/build_your_room/templates/base.html` — added "Repos" nav link
- `static/style.css` — repo card, repo detail, and action link styles
- `tests/test_routes_repos.py` — 16 new tests (10 list page + 4 detail enhancements + 2 PBT)
- `docs/plans/build-your-room-tasks.md` — added Tasks 33-34

## Test count

16 new tests, 996 total (994 passing + 2 pre-existing Hypothesis flakes)
