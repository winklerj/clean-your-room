# Database Schema

The application uses a single SQLite database file (`clean_room.db`) located in the `CLEAN_ROOM_DIR` directory. The database is accessed asynchronously via `aiosqlite`.

## Connection Settings

- **Journal mode:** WAL (Write-Ahead Logging), set on each connection for improved concurrent read performance.
- **Foreign keys:** Enabled on each connection (`PRAGMA foreign_keys = ON`).

## Tables

### `repos`

Stores GitHub repositories that have been added to the application.

| Column       | Type    | Constraints              | Default          |
|--------------|---------|--------------------------|------------------|
| `id`         | INTEGER | PRIMARY KEY AUTOINCREMENT| —                |
| `github_url` | TEXT    | NOT NULL                 | —                |
| `org`        | TEXT    | NOT NULL                 | —                |
| `repo_name`  | TEXT    | NOT NULL                 | —                |
| `slug`       | TEXT    | NOT NULL UNIQUE          | —                |
| `clone_path` | TEXT    | NOT NULL                 | —                |
| `status`     | TEXT    | NOT NULL                 | `'active'`       |
| `created_at` | TEXT    | NOT NULL                 | `datetime('now')` |

**Status values:** `active`, `archived`.

The `slug` column uses the format `{org}--{repo_name}` and serves as a unique identifier for the repository within the application. The `clone_path` column stores the absolute filesystem path to the cloned repository.

---

### `prompts`

Stores prompt templates used by the agent when running jobs.

| Column       | Type    | Constraints              | Default          |
|--------------|---------|--------------------------|------------------|
| `id`         | INTEGER | PRIMARY KEY AUTOINCREMENT| —                |
| `name`       | TEXT    | NOT NULL                 | —                |
| `template`   | TEXT    | NOT NULL                 | —                |
| `created_at` | TEXT    | NOT NULL                 | `datetime('now')` |
| `updated_at` | TEXT    | NOT NULL                 | `datetime('now')` |

**Default prompt seeding:** On application startup, if the `prompts` table contains zero rows, a set of built-in default prompts is inserted. This seeding only occurs when the table is completely empty; existing prompts are never modified or replaced.

---

### `jobs`

Stores job records. Each job represents a single agent execution against a repository using a specific prompt.

| Column                | Type    | Constraints                | Default          |
|-----------------------|---------|----------------------------|------------------|
| `id`                  | INTEGER | PRIMARY KEY AUTOINCREMENT  | —                |
| `repo_id`             | INTEGER | NOT NULL, FK -> `repos(id)` | —                |
| `feature_description` | TEXT    | —                          | NULL             |
| `prompt_id`           | INTEGER | NOT NULL, FK -> `prompts(id)` | —             |
| `max_iterations`      | INTEGER | NOT NULL                   | `20`             |
| `status`              | TEXT    | NOT NULL                   | `'pending'`      |
| `current_iteration`   | INTEGER | NOT NULL                   | `0`              |
| `created_at`          | TEXT    | NOT NULL                   | `datetime('now')` |
| `started_at`          | TEXT    | —                          | NULL             |
| `completed_at`        | TEXT    | —                          | NULL             |

**Status values:** `pending`, `running`, `completed`, `stopped`, `failed`.

**Foreign keys:**
- `repo_id` references `repos(id)`.
- `prompt_id` references `prompts(id)`.

**Status lifecycle:** A job is created with status `pending`. When the background runner picks it up, the status changes to `running` and `started_at` is set. The job ends in one of three terminal states:
- `completed` — The agent finished all iterations or determined it was done.
- `stopped` — A user triggered the stop action.
- `failed` — An unhandled error occurred during execution.

In all terminal cases, `completed_at` is set.

---

### `job_logs`

Stores log entries produced during job execution. Each row corresponds to one log entry from a single iteration.

| Column      | Type    | Constraints                | Default          |
|-------------|---------|----------------------------|------------------|
| `id`        | INTEGER | PRIMARY KEY AUTOINCREMENT  | —                |
| `job_id`    | INTEGER | NOT NULL, FK -> `jobs(id)` | —                |
| `iteration` | INTEGER | NOT NULL                   | —                |
| `content`   | TEXT    | NOT NULL                   | —                |
| `timestamp` | TEXT    | NOT NULL                   | `datetime('now')` |

**Foreign keys:**
- `job_id` references `jobs(id)`.

Logs are written as the agent runs and are also streamed in real time to connected clients via the SSE endpoint (`GET /jobs/{job_id}/stream`).
