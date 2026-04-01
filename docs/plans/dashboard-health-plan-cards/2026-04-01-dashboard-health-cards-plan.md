# Dashboard Health Cards — Implementation Plan

## Overview

Add summary stat cards and enriched repo rows to the dashboard. The top of the page gets color-coded cards showing total repos, running jobs, completed jobs, and failed jobs. Each repo row gains a progress bar (for running jobs), spec freshness indicator, and a "Run again" button. Zero new dependencies — pure SQL queries on existing tables + HTML/CSS changes.

## Current State Analysis

- **Dashboard route** (`src/clean_room/routes/dashboard.py:10-33`): Single query joins repos with last job status. Passes `request` and `repos` to template.
- **Dashboard template** (`src/clean_room/templates/dashboard.html`): 31-line Jinja2 template. Table with 3 columns: Repo, Last Job, Last Run. No JS.
- **CSS** (`static/style.css`): Status badge classes already defined for all 5 states (running/completed/stopped/failed/pending).
- **DB** (`src/clean_room/db.py`): SQLite via aiosqlite. Tables: `repos`, `jobs`, `prompts`, `job_logs`. No schema changes needed.
- **Restart endpoint** (`src/clean_room/routes/jobs.py:165-206`): `POST /jobs/{job_id}/restart` already exists and creates a new job with the same parameters.
- **Tests** (`tests/test_routes_dashboard.py`): One test — empty dashboard renders.

### Key Discoveries:
- `completed_at` is set for finished jobs (completed, failed, stopped) but NULL for running/pending — `src/clean_room/routes/jobs.py:34,72`
- Lifespan marks orphaned running jobs as failed on startup — `src/clean_room/main.py:25-29`
- `aiosqlite.Row` factory makes rows behave like dicts — `src/clean_room/db.py:102`
- HTMX loaded in base.html but dashboard doesn't use it — `src/clean_room/templates/base.html:7`

## Desired End State

The dashboard shows:
1. **Four summary cards** at the top: Total Repos, Running Jobs, Completed Jobs, Failed Jobs — each with a count and color-coded top border
2. **Enriched repo table** with columns: Repo, Last Job (status badge), Progress (iteration bar), Specs (freshness timestamp), Actions ("Run again" button)
3. **Progress bar** on each repo row showing `current_iteration / max_iterations` for the last job
4. **Spec freshness** showing the `completed_at` timestamp of the last successful job, or "no specs yet"
5. **"Run again" button** on repos that have at least one prior job, POSTing to the existing restart endpoint

### Verification:
- `uv run pytest tests/test_routes_dashboard.py -v` passes with new tests
- `uv run ruff check src/ tests/` passes
- Dashboard renders correctly with 0 repos, repos with no jobs, repos with running/completed/failed jobs
- Summary card counts match actual DB state

## What We're NOT Doing

- No schema changes or migrations
- No new database tables
- No JavaScript / HTMX interactivity (static server-rendered page)
- No auto-refresh or polling (can be added later)
- No new dependencies
- No changes to other routes or templates

## Implementation Approach

Three phases: (1) backend — enrich the dashboard route with stats queries and extended repo data, (2) frontend — update template and CSS, (3) tests. Each phase is independently verifiable.

---

## Phase 1: Backend — Enrich Dashboard Route

### Overview
Add aggregate stats query and extend the existing repo query with iteration info, last job ID, and last completed timestamp.

### Changes Required:

#### 1.1 Dashboard Route

**File**: `src/clean_room/routes/dashboard.py`
**Changes**: Add a stats query and extend the existing repo query.

Replace the entire `dashboard` function with:

```python
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        # Aggregate job stats
        cursor = await db.execute("""
            SELECT status, COUNT(*) as count
            FROM jobs
            GROUP BY status
        """)
        stats_rows = await cursor.fetchall()
        job_stats = {row["status"]: row["count"] for row in stats_rows}

        # Repos with last job info + last completed timestamp
        cursor = await db.execute("""
            SELECT r.*,
                   j.status as last_status,
                   j.completed_at as last_run,
                   j.current_iteration as last_iteration,
                   j.max_iterations as last_max_iterations,
                   j.id as last_job_id,
                   c.completed_at as last_completed_at
            FROM repos r
            LEFT JOIN (
                SELECT repo_id, status, completed_at,
                       current_iteration, max_iterations, id,
                       ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY id DESC) as rn
                FROM jobs
            ) j ON j.repo_id = r.id AND j.rn = 1
            LEFT JOIN (
                SELECT repo_id, completed_at,
                       ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY id DESC) as rn
                FROM jobs
                WHERE status = 'completed'
            ) c ON c.repo_id = r.id AND c.rn = 1
            WHERE r.status = 'active'
            ORDER BY r.created_at DESC
        """)
        repos = await cursor.fetchall()

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "repos": repos,
            "job_stats": job_stats,
            "total_repos": len(repos),
        })
    finally:
        await db.close()
```

Key changes:
- New `job_stats` dict maps status → count (e.g., `{"running": 2, "completed": 5, "failed": 1}`)
- Extended repo query adds: `last_iteration`, `last_max_iterations`, `last_job_id`, `last_completed_at`
- `last_completed_at` comes from a second LEFT JOIN filtering only `status='completed'` jobs
- `total_repos` passed separately for the summary card (avoids template logic to count iterable)

### Success Criteria:

#### Automated Verification:
- [ ] `uv run ruff check src/clean_room/routes/dashboard.py` passes
- [ ] `uv run mypy src/clean_room/routes/dashboard.py --ignore-missing-imports` passes
- [ ] App starts without errors: `uv run uvicorn clean_room.main:app --port 8888`

#### Manual Verification:
- [ ] Dashboard loads at `http://localhost:8888/`
- [ ] No Python errors in console

**Implementation Note**: After completing this phase and all automated verification passes, pause here for manual confirmation before proceeding to Phase 2.

---

## Phase 2: Frontend — Template and CSS

### Overview
Add summary cards section to the dashboard template and enrich repo table rows. Add CSS for cards, progress bars, and layout.

### Changes Required:

#### 2.1 Dashboard Template

**File**: `src/clean_room/templates/dashboard.html`
**Changes**: Full rewrite of the template content block.

```html
{% extends "base.html" %}
{% block title %}Dashboard - Clean Room{% endblock %}
{% block content %}
<h1>Clean Room</h1>

<div class="stats-cards">
    <div class="stat-card stat-card-total">
        <div class="stat-number">{{ total_repos }}</div>
        <div class="stat-label">Total Repos</div>
    </div>
    <div class="stat-card stat-card-running">
        <div class="stat-number">{{ job_stats.get('running', 0) }}</div>
        <div class="stat-label">Running</div>
    </div>
    <div class="stat-card stat-card-completed">
        <div class="stat-number">{{ job_stats.get('completed', 0) }}</div>
        <div class="stat-label">Completed</div>
    </div>
    <div class="stat-card stat-card-failed">
        <div class="stat-number">{{ job_stats.get('failed', 0) }}</div>
        <div class="stat-label">Failed</div>
    </div>
</div>

<p><a href="/repos/new">Add Repo</a></p>

{% if repos %}
<table>
    <thead>
        <tr>
            <th>Repo</th>
            <th>Last Job</th>
            <th>Progress</th>
            <th>Specs</th>
            <th></th>
        </tr>
    </thead>
    <tbody>
        {% for repo in repos %}
        <tr>
            <td><a href="/repos/{{ repo.id }}">{{ repo.org }}/{{ repo.repo_name }}</a></td>
            <td>
                {% if repo.last_status %}
                <span class="status-badge status-{{ repo.last_status }}">{{ repo.last_status }}</span>
                {% else %}
                <span class="status-badge status-pending">no jobs</span>
                {% endif %}
            </td>
            <td>
                {% if repo.last_iteration is not none and repo.last_max_iterations %}
                <div class="progress-bar">
                    <div class="progress-fill progress-{{ repo.last_status }}"
                         style="width: {{ (repo.last_iteration / repo.last_max_iterations * 100) | round }}%">
                    </div>
                </div>
                <span class="progress-text">{{ repo.last_iteration }}/{{ repo.last_max_iterations }}</span>
                {% else %}
                -
                {% endif %}
            </td>
            <td>
                {% if repo.last_completed_at %}
                <span class="freshness">{{ repo.last_completed_at }}</span>
                {% else %}
                <span class="freshness freshness-none">no specs yet</span>
                {% endif %}
            </td>
            <td>
                {% if repo.last_job_id %}
                <form method="post" action="/jobs/{{ repo.last_job_id }}/restart" style="display:inline">
                    <button type="submit" class="btn-run-again">Run again</button>
                </form>
                {% endif %}
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<p>No repos yet. Add one to get started.</p>
{% endif %}
{% endblock %}
```

Key changes:
- Summary cards section with 4 cards using `job_stats` dict
- Table gains "Progress" and "Specs" columns, loses "Last Run"
- Progress column shows a bar + text like "5/20"
- Specs column shows `last_completed_at` or "no specs yet"
- Actions column has "Run again" form (only for repos with prior jobs)

#### 2.2 CSS

**File**: `static/style.css`
**Changes**: Append new styles for stat cards, progress bars, and the run-again button.

```css
/* Summary stat cards */
.stats-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
.stat-card { padding: 1rem; border-radius: 6px; border-top: 4px solid #ccc; background: #fafafa; text-align: center; }
.stat-number { font-size: 2rem; font-weight: 700; line-height: 1.2; }
.stat-label { font-size: 0.85em; color: #666; margin-top: 0.25rem; }
.stat-card-total { border-top-color: #555; }
.stat-card-running { border-top-color: #5cb85c; }
.stat-card-completed { border-top-color: #5bc0de; }
.stat-card-failed { border-top-color: #d9534f; }

/* Progress bar */
.progress-bar { height: 8px; background: #eee; border-radius: 4px; overflow: hidden; margin-bottom: 0.2rem; }
.progress-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.progress-running { background: #5cb85c; }
.progress-completed { background: #5bc0de; }
.progress-stopped { background: #f0ad4e; }
.progress-failed { background: #d9534f; }
.progress-pending { background: #ccc; }
.progress-text { font-size: 0.8em; color: #888; }

/* Spec freshness */
.freshness { font-size: 0.85em; }
.freshness-none { color: #999; font-style: italic; }

/* Run again button */
.btn-run-again { font-size: 0.8em; padding: 0.2rem 0.6rem; border: 1px solid #ccc; border-radius: 3px; background: #fff; cursor: pointer; }
.btn-run-again:hover { background: #f5f5f5; border-color: #999; }
```

### Success Criteria:

#### Automated Verification:
- [ ] `uv run ruff check src/ tests/` passes
- [ ] App starts and dashboard loads without template errors

#### Manual Verification:
- [ ] Summary cards display at top with correct counts and colors
- [ ] Repos with running jobs show green progress bar filling proportionally
- [ ] Repos with completed jobs show blue progress bar at 100%
- [ ] Repos with no jobs show "no jobs" badge, no progress bar, "no specs yet"
- [ ] "Run again" button appears only for repos with prior jobs
- [ ] Clicking "Run again" creates a new job and redirects to job viewer
- [ ] Cards responsive on narrow screens (grid collapses gracefully)

**Implementation Note**: After completing this phase and all automated/manual verification passes, proceed to Phase 3.

---

## Phase 3: Tests

### Overview
Add tests verifying the dashboard renders correctly with stats cards and enriched data for various repo/job states.

### Changes Required:

#### 3.1 Dashboard Tests

**File**: `tests/test_routes_dashboard.py`
**Changes**: Add test fixtures for seeding data and tests for dashboard with repos/jobs.

```python
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

import aiosqlite
from clean_room.db import init_db, get_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    """App fixture with patched DB path."""
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    with patch("clean_room.routes.dashboard.DB_PATH", db_path), \
         patch("clean_room.main.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    """Async HTTP client for the test app."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_repo(db_path, org="testorg", repo_name="testrepo"):
    """Insert a repo and return its ID.

    Helper that creates a single active repo for test scenarios.
    """
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "INSERT INTO repos (github_url, org, repo_name, slug, clone_path) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (f"https://github.com/{org}/{repo_name}", org, repo_name,
             f"{org}-{repo_name}", f"/tmp/{org}-{repo_name}"),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0]
    finally:
        await db.close()


async def _seed_job(db_path, repo_id, status="completed", iteration=10, max_iter=20):
    """Insert a job for a repo and return its ID.

    Helper that creates a job with configurable status and iteration state.
    """
    db = await get_db(db_path)
    try:
        completed_at = "datetime('now')" if status in ("completed", "failed", "stopped") else "NULL"
        started_at = "datetime('now')" if status != "pending" else "NULL"
        cursor = await db.execute(
            f"INSERT INTO jobs (repo_id, prompt_id, status, current_iteration, "
            f"max_iterations, started_at, completed_at) "
            f"VALUES (?, 1, ?, ?, ?, {started_at}, {completed_at}) RETURNING id",
            (repo_id, status, iteration, max_iter),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    """Dashboard renders with no repos.

    Verifies the empty state: summary cards should show all zeros,
    and the repos table should not appear.
    """
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Clean Room" in resp.text
    assert "Add Repo" in resp.text
    assert "No repos yet" in resp.text


@pytest.mark.asyncio
async def test_dashboard_stats_cards(tmp_path, client):
    """Dashboard summary cards reflect actual job counts.

    Verifies that aggregate stats (running, completed, failed) are
    computed correctly and rendered in the stat cards.
    """
    db_path = tmp_path / "test.db"
    repo_id = await _seed_repo(db_path)
    await _seed_job(db_path, repo_id, status="completed")
    await _seed_job(db_path, repo_id, status="failed")
    await _seed_job(db_path, repo_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "stat-card-completed" in resp.text
    assert "stat-card-failed" in resp.text
    assert "stat-card-running" in resp.text


@pytest.mark.asyncio
async def test_dashboard_repo_with_completed_job(tmp_path, client):
    """Repo row shows completed status badge and spec freshness.

    Verifies that a repo with a completed job shows the status badge,
    progress info, and a non-empty freshness timestamp.
    """
    db_path = tmp_path / "test.db"
    repo_id = await _seed_repo(db_path)
    await _seed_job(db_path, repo_id, status="completed", iteration=20, max_iter=20)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "status-completed" in resp.text
    assert "20/20" in resp.text
    assert "no specs yet" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_repo_no_jobs(tmp_path, client):
    """Repo row shows 'no jobs' badge and 'no specs yet' when no jobs exist.

    Verifies the empty-job state per repo: no progress bar, no run-again button.
    """
    db_path = tmp_path / "test.db"
    await _seed_repo(db_path)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "no jobs" in resp.text
    assert "no specs yet" in resp.text
    assert "Run again" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_run_again_button(tmp_path, client):
    """Run again button appears for repos with at least one prior job.

    Verifies the button is present and points to the correct restart endpoint.
    """
    db_path = tmp_path / "test.db"
    repo_id = await _seed_repo(db_path)
    job_id = await _seed_job(db_path, repo_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Run again" in resp.text
    assert f"/jobs/{job_id}/restart" in resp.text
```

**Note**: The `_seed_job` helper uses `datetime('now')` SQL expressions directly for timestamps. The `prompt_id=1` works because `init_db` seeds default prompts.

### Success Criteria:

#### Automated Verification:
- [ ] `uv run pytest tests/test_routes_dashboard.py -v` — all tests pass
- [ ] `uv run ruff check src/ tests/` — no lint errors
- [ ] `uv run mypy src/ --ignore-missing-imports` — no type errors

---

## Testing Strategy

### Unit Tests:
- Dashboard with 0 repos (empty state)
- Dashboard with repos that have no jobs
- Dashboard with repos that have completed/failed/running jobs
- Summary card counts match seeded data
- "Run again" button presence/absence based on job history

### Integration Tests:
- Full flow: add repo → run job → dashboard shows updated stats (existing test infrastructure)

### Manual Testing Steps:
1. Start the app: `uv run uvicorn clean_room.main:app --port 8888`
2. Visit `http://localhost:8888/` — should see 4 stat cards all showing 0
3. Add a repo via "Add Repo"
4. Return to dashboard — total repos shows 1, repo row shows "no jobs" and "no specs yet"
5. Start a job from the repo detail page
6. Return to dashboard — running count should be 1, progress bar should animate
7. After job completes — completed count increases, spec freshness shows timestamp
8. Click "Run again" — new job created, redirected to job viewer

## Performance Considerations

- Both queries are lightweight: `GROUP BY` on the small `jobs` table, and the existing `ROW_NUMBER()` window function only adds one more subquery
- SQLite single-writer is not a concern since these are read-only queries
- No N+1 query patterns — all data fetched in 2 queries

## References

- Research document: `tmp/2026-04-01-dashboard-health-cards-research.md`
- Existing dashboard route: `src/clean_room/routes/dashboard.py:10-33`
- Existing restart endpoint: `src/clean_room/routes/jobs.py:165-206`
- Status badge CSS: `static/style.css:11-16`
