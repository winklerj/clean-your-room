# Project Structure

```
src/clean_room/
├── __init__.py
├── main.py
├── config.py
├── models.py
├── db.py
├── git_ops.py
├── streaming.py
├── runner.py
├── routes/
│   ├── dashboard.py
│   ├── prompts.py
│   ├── repos.py
│   └── jobs.py
└── templates/
    ├── base.html
    ├── dashboard.html
    ├── add_repo.html
    ├── repo_detail.html
    ├── prompts.html
    ├── job_viewer.html
    └── partials/
        ├── prompt_form.html
        └── prompt_row.html

tests/
static/style.css
```

## Modules

### `main.py`

Application entry point. Defines the FastAPI app instance, the lifespan handler (which runs startup initialization), and registers all route modules.

### `config.py`

Reads environment variables and exposes path configuration. Provides the base data directory (`CLEAN_ROOM_DIR`), the default model name (`DEFAULT_MODEL`), and derived paths for the repos directory, specs monorepo, and database file.

### `models.py`

Contains the `GitHubUrl` dataclass and the `parse_github_url()` function. `parse_github_url()` extracts the organization and repository name from a GitHub URL and returns a `GitHubUrl` instance.

### `db.py`

Manages the SQLite database. Contains the schema DDL, the `init_db()` function for creating tables and seeding default prompts, and the `get_db()` async dependency for obtaining a database connection with WAL mode and foreign keys enabled.

### `git_ops.py`

Git operations for repository management. Provides `clone_repo()` to clone a GitHub repository to the local filesystem, `pull_repo()` to update an existing clone, `init_specs_monorepo()` to initialize the specs aggregation repository, and `commit_specs()` to commit generated specs into the monorepo.

### `streaming.py`

Implements the `LogBuffer` class, a pub/sub mechanism for real-time log delivery. Job runners publish log entries to a buffer, and SSE clients subscribe to receive those entries as they arrive.

### `runner.py`

Implements the `JobRunner` class. Manages the iterative execution of a Claude agent against a repository, respecting `max_iterations` and the cancel event. Updates job status and logs in the database as the agent runs.

## Routes

### `routes/dashboard.py`

Serves the main dashboard (`GET /`) and the add-repo form page (`GET /repos/new`).

### `routes/prompts.py`

Full CRUD for prompt templates. Supports listing, creating, updating, deleting prompts, and serving edit-form and display-row partials for inline editing.

### `routes/repos.py`

Handles repository management: adding a repo from a GitHub URL (`POST /repos`), viewing repo detail with job history (`GET /repos/{repo_id}`), and archiving a repo (`POST /repos/{repo_id}/archive`).

### `routes/jobs.py`

Job lifecycle endpoints: creating and starting jobs, viewing the job page, stopping and restarting jobs, and the SSE stream for real-time log delivery.

## Templates

### Layout

- `base.html` — Base layout template. All other full-page templates extend this.

### Pages

- `dashboard.html` — Main dashboard listing active repositories and their latest job status.
- `add_repo.html` — Form for adding a new GitHub repository.
- `repo_detail.html` — Repository detail page with job history and prompt selection.
- `prompts.html` — Prompt management page with inline editing.
- `job_viewer.html` — Job detail page with status, configuration, and live log output.

### Partials

- `partials/prompt_form.html` — Inline edit form for a single prompt (used by HTMX swap).
- `partials/prompt_row.html` — Display row for a single prompt (used by HTMX swap).

## Other Files

### `tests/`

Pytest test suite. Includes unit tests for database operations, Git operations, models, and route handlers, as well as integration tests.

### `static/style.css`

Application stylesheet.
