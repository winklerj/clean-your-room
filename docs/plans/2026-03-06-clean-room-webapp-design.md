# Clean Room Webapp Design

Personal webapp for generating formal verification specs from GitHub repos using iterative Claude Agent SDK loops.

## Stack

- **Backend:** FastAPI + HTMX (server-rendered), Python 3.12, uv
- **Database:** SQLite via aiosqlite
- **AI:** Claude Agent SDK (`claude_agent_sdk`)
- **Frontend:** HTMX + Jinja2 templates, SSE for live streaming

## Data Model

### Repos

| Column     | Type    | Notes                                    |
|------------|---------|------------------------------------------|
| id         | INTEGER | PK                                       |
| github_url | TEXT    | Full GitHub URL                          |
| org        | TEXT    | GitHub org/user                          |
| repo_name  | TEXT    | Repository name                          |
| slug       | TEXT    | `{org}--{repo_name}`, unique             |
| clone_path | TEXT    | Persistent local clone path              |
| status     | TEXT    | `active` / `archived`                    |
| created_at | TEXT    | ISO 8601                                 |

### Prompts

| Column     | Type    | Notes                                    |
|------------|---------|------------------------------------------|
| id         | INTEGER | PK                                       |
| name       | TEXT    | Display name                             |
| template   | TEXT    | Markdown, supports `${PLAN_FILE}` subst  |
| created_at | TEXT    | ISO 8601                                 |
| updated_at | TEXT    | ISO 8601                                 |

Seeded with two prompts:
1. **Create Spec** — from `prompt.md` (create new spec)
2. **Improve Spec** — from `improvement-prompt.md` (improve existing spec)

### Jobs

| Column              | Type    | Notes                                    |
|---------------------|---------|------------------------------------------|
| id                  | INTEGER | PK                                       |
| repo_id             | INTEGER | FK → Repos                               |
| feature_description | TEXT    | Nullable — whole repo if blank           |
| prompt_id           | INTEGER | FK → Prompts                             |
| max_iterations      | INTEGER | Default 20                               |
| status              | TEXT    | `pending`/`running`/`stopped`/`completed`/`failed` |
| current_iteration   | INTEGER | Tracks progress                          |
| created_at          | TEXT    | ISO 8601                                 |
| started_at          | TEXT    | ISO 8601, nullable                       |
| completed_at        | TEXT    | ISO 8601, nullable                       |

### Job Logs

| Column    | Type    | Notes                                    |
|-----------|---------|------------------------------------------|
| id        | INTEGER | PK                                       |
| job_id    | INTEGER | FK → Jobs                                |
| iteration | INTEGER | Which iteration produced this             |
| content   | TEXT    | Raw Claude output                        |
| timestamp | TEXT    | ISO 8601                                 |

Append-only, streamed to frontend via SSE.

## Architecture

```
Browser (HTMX)
  ├── Dashboard (repo list)
  ├── Repo Detail (jobs + new job form)
  ├── Job Viewer (SSE log stream)
  ├── Prompts (CRUD)
  └── Add Repo
        │
        ▼ HTTP + SSE
FastAPI Server
  ├── Route handlers (Jinja2 templates + HTMX partials)
  ├── SSE endpoint (per-job log streaming)
  ├── SQLite via aiosqlite (repos, jobs, prompts, logs)
  ├── In-memory log buffer (dict[job_id, deque])
  ├── Cancellation flags (dict[job_id, asyncio.Event])
  └── Job Runner (asyncio background tasks)
        │
        ├── Persistent repo clones (~/.clean-room/repos/{org}--{repo}/)
        └── Specs monorepo (~/.clean-room/specs-monorepo/{org}--{repo}/)
```

### Job Runner Flow

1. `git pull` the persistent clone (or initial clone if first run)
2. Load prompt template, substitute variables
3. Iteration loop (up to `max_iterations`):
   - Check cancellation event → break if set
   - Run Claude Agent SDK agent with filesystem tools scoped to clone dir
   - Stream output to in-memory log buffer + persist to SQLite
   - Update `current_iteration` in DB
4. Copy generated specs from clone to specs monorepo under `{org}--{repo}/`
5. Git commit to specs monorepo

### Cancellation

- Each running job has an `asyncio.Event` in `active_jobs: dict[int, asyncio.Event]`
- **Between iterations:** check `cancel_event.is_set()` before next Claude call
- **Mid-execution:** wrap Agent SDK call in `asyncio.Task`, call `.cancel()` on stop
- **Graceful shutdown:** commit partial specs with message like `"Partial specs for {org}/{repo} (stopped at iteration 3/20)"`
- Job status becomes `stopped` (not `failed`)

### Restart

Creates a new job on the same repo. The persistent clone already has specs from previous runs, so the agent picks up where it left off (prompt says "implement the next incomplete item").

## Pages

### 1. Dashboard (`/`)
- List of active repos: slug, last job status, last run date
- "Add Repo" button
- Each row links to repo detail

### 2. Add Repo (`/repos/new`)
- GitHub URL input → auto-parses org/repo
- Clones on submit, redirects to repo detail

### 3. Repo Detail (`/repos/{id}`)
- Header: org/repo, GitHub link, clone status
- "New Job" inline form: select prompt, optional feature description, max iterations
- "Archive" button
- Jobs table: all jobs for this repo (recent first), status, iteration count, timestamp
- Each job row links to job viewer

### 4. Job Viewer (`/jobs/{id}`)
- Status bar: job status, current iteration / max, GitHub URL, prompt used
- Stop button (when running)
- Restart button (when stopped/failed)
- Log stream: monospace scrolling div, auto-scroll, populated via SSE
- Breadcrumb back to repo detail

### 5. Prompts (`/prompts`)
- Table with name and preview
- Inline create/edit/delete via HTMX partials
- Textarea editor with monospace font

## File Layout

```
src/clean_room/
  __init__.py
  main.py              # FastAPI app, lifespan, mount static
  db.py                # SQLite schema, migrations, aiosqlite helpers
  models.py            # Pydantic models for repos, jobs, prompts
  routes/
    __init__.py
    dashboard.py       # GET /
    repos.py           # /repos/new, /repos/{id}, /repos/{id}/archive
    jobs.py            # /jobs/new, /jobs/{id}, /jobs/{id}/stop, /jobs/{id}/stream
    prompts.py         # /prompts CRUD
  runner.py            # Job execution loop, Claude Agent SDK integration
  streaming.py         # SSE helpers, in-memory log buffer
  git_ops.py           # Clone, pull, commit helpers for repos + specs monorepo
  templates/
    base.html
    dashboard.html
    repo_detail.html
    job_viewer.html
    prompts.html
    partials/          # HTMX partial templates
static/
  style.css
```

## Specs Monorepo Structure

```
~/.clean-room/specs-monorepo/
  {org}--{repo}/
    spec-001.md
    spec-002.md
    ...
  {org}--{repo}/
    spec-001.md
    ...
```

Flat by org--repo slug. Each spec file is a clean room specification including:
- Provable Properties Catalog
- Purity Boundary Map
- Verification Tooling Selection
- Property Specifications (formal definitions)

## Configuration

Minimal config via environment variables or a `.env` file:
- `CLEAN_ROOM_DIR` — base directory (default `~/.clean-room/`)
- `ANTHROPIC_API_KEY` — for Claude Agent SDK
- `DEFAULT_MODEL` — Claude model to use (default `claude-sonnet-4-20250514`)
