---
date: 2026-04-01T14:43:08Z
researcher: robbwinkle
git_commit: 0531ba88e0f5d379b250151997a8447996e680ec
branch: main
repository: clean-your-room
topic: "Dashboard health cards - codebase research for implementation"
tags: [research, codebase, dashboard, routes, templates, css, db]
status: complete
last_updated: 2026-04-01
last_updated_by: robbwinkle
---

# Research: Dashboard Health Cards Implementation Context

**Date**: 2026-04-01T14:43:08Z
**Researcher**: robbwinkle
**Git Commit**: 0531ba88
**Branch**: main
**Repository**: clean-your-room

## Research Question

What parts of the codebase are involved in implementing dashboard health cards (summary stats, status indicators, freshness signals)?

## Summary

The dashboard feature touches 4 files that need modification: a route handler, a template, CSS, and potentially the database module. The current dashboard is minimal (single SQL query, simple table). All data needed for health cards already exists in the `jobs` and `repos` tables -- no schema changes needed.

## Detailed Findings

### 1. Dashboard Route (`src/clean_room/routes/dashboard.py`)

**What exists:** A single `GET /` endpoint (40 lines total, 2 routes).

The dashboard query (lines 15-27) already does a LEFT JOIN to get the last job status per repo:

```sql
SELECT r.*,
       j.status as last_status,
       j.completed_at as last_run
FROM repos r
LEFT JOIN (
    SELECT repo_id, status, completed_at,
           ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY id DESC) as rn
    FROM jobs
) j ON j.repo_id = r.id AND j.rn = 1
WHERE r.status = 'active'
ORDER BY r.created_at DESC
```

**Key observations:**
- Uses `aiosqlite.Row` factory (set in `get_db`), so rows behave like dicts
- Opens/closes its own DB connection (no shared connection pool)
- Template context currently only passes `request` and `repos`
- The `last_status` and `last_run` fields are already available per repo

**What needs to change:** Add a second query (or queries) to compute aggregate stats, then pass them to the template context.

### 2. Dashboard Template (`src/clean_room/templates/dashboard.html`)

**What exists:** A minimal Jinja2 template (31 lines):
- Extends `base.html`
- Shows an "Add Repo" link
- Renders a table with 3 columns: Repo, Last Job, Last Run
- Each repo row shows: org/repo_name link, status badge (or "no jobs"), last_run timestamp
- Empty state: "No repos yet" message

**Key observations:**
- Status badges already use the pattern `<span class="status-badge status-{{ status }}">` which maps to CSS classes
- The template has no JavaScript, no HTMX attributes (purely static)
- Content lives inside `{% block content %}`

**What needs to change:** Add a stats summary section above the table. Could also enrich each repo row.

### 3. Base Template (`src/clean_room/templates/base.html`)

**What exists:** 20-line layout with nav (Dashboard, Prompts links), HTMX scripts, stylesheet link. No custom JS in the base.

### 4. CSS (`static/style.css`)

**What exists:** 30 lines of CSS covering:
- Body layout: `system-ui` font, 960px max-width, centered
- Nav: flex, gap, border-bottom
- Tables: full width, collapsed borders
- Form elements: full-width inputs, block labels
- Status badges: `.status-badge` with variants for each status:
  - `.status-running` - green (`#dff0d8`)
  - `.status-completed` - blue (`#d9edf7`)
  - `.status-stopped` - yellow (`#fcf8e3`)
  - `.status-failed` - red (`#f2dede`)
  - `.status-pending` - grey (`#eee`)
- Log stream styles (dark theme, monospace)

**What needs to change:** Add CSS for stat cards / summary section. The status badge colors are already defined and can be reused.

### 5. Database Schema (`src/clean_room/db.py`)

**Relevant tables and columns for stats queries:**

**repos:**
- `id`, `status` ('active'/'archived'), `created_at`

**jobs:**
- `id`, `repo_id` (FK), `status` ('pending'/'running'/'completed'/'stopped'/'failed'), `current_iteration`, `max_iterations`, `created_at`, `started_at`, `completed_at`

**job_logs:**
- `id`, `job_id` (FK), `iteration`, `content`, `timestamp`

**Stats derivable from existing data (no schema changes):**
- Total active repos: `COUNT(*) FROM repos WHERE status='active'`
- Total jobs by status: `COUNT(*) FROM jobs GROUP BY status`
- Currently running jobs: `COUNT(*) FROM jobs WHERE status='running'`
- Repos with no jobs: repos LEFT JOIN jobs WHERE job is NULL
- Repos with no completed jobs (need specs): repos with no `completed` job
- Total iterations run: `SUM(current_iteration) FROM jobs`
- Last job completion time (freshness)

### 6. Active Jobs Tracking (`src/clean_room/routes/jobs.py`)

**What exists:** Module-level dictionaries track in-memory state:
```python
active_jobs: dict[int, asyncio.Event] = {}  # cancel events
running_tasks: dict[int, asyncio.Task] = {}  # asyncio tasks
```

These could be consulted for "currently running" count without a DB query, but DB is more reliable (survives restarts since lifespan marks stale running jobs as failed).

### 7. Application Lifespan (`src/clean_room/main.py`)

**What exists:** On startup (lines 17-32):
- Creates directories
- Initializes DB schema
- Marks any orphaned "running" jobs as "failed" (crash recovery)

This means `jobs.status='running'` in the DB is always accurate after startup.

## Code References

- `src/clean_room/routes/dashboard.py:10-33` - Dashboard route handler
- `src/clean_room/templates/dashboard.html:1-31` - Dashboard template
- `src/clean_room/templates/base.html:1-20` - Base layout
- `static/style.css:1-30` - All styles including status badges
- `src/clean_room/db.py:6-44` - Schema definition
- `src/clean_room/routes/jobs.py:15-17` - In-memory active job tracking
- `src/clean_room/main.py:17-32` - Lifespan startup logic

## Architecture Documentation

### Patterns to follow:
1. **DB access pattern**: Each route opens its own connection via `get_db()`, uses try/finally to close
2. **Template context**: Pass `request` + data dicts to `templates.TemplateResponse()`
3. **Status badges**: Use `class="status-badge status-{status}"` pattern
4. **Template structure**: Extend `base.html`, use `{% block content %}`
5. **CSS organization**: Flat file, no preprocessor, single-purpose classes

### Constraints:
- SQLite single-writer: aggregate queries should be fast since tables are small
- No connection pooling: each request opens/closes a connection
- HTMX available in all pages (loaded in base.html) but dashboard currently doesn't use it
- No JavaScript framework; inline scripts only where needed
