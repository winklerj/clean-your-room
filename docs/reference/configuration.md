# Configuration

## Environment Variables

| Variable         | Default                    | Description                            |
|------------------|----------------------------|----------------------------------------|
| `CLEAN_ROOM_DIR` | `~/.clean-room`            | Base directory for all application data |
| `DEFAULT_MODEL`  | `claude-sonnet-4-20250514` | Default Claude model used by the agent |

Environment variables can be set in a `.env` file in the project root. The application loads them via `python-dotenv` at startup.

## Directory Structure

All persistent data is stored under the path specified by `CLEAN_ROOM_DIR`. The default location is `~/.clean-room`.

```
$CLEAN_ROOM_DIR/
├── repos/              # Cloned GitHub repositories
│                       # Named using org--repo format (e.g. acme--widget)
├── specs-monorepo/     # Git repository aggregating generated specs
└── clean_room.db       # SQLite database
```

### `repos/`

Each cloned repository is stored in a subdirectory named with the pattern `{org}--{repo_name}`. This directory is created automatically when a repository is added through the application.

### `specs-monorepo/`

A local Git repository that collects the specs produced by agent jobs. Initialized automatically if not already present.

### `clean_room.db`

The SQLite database file containing all application state. See the [Database Schema](database-schema.md) reference for full details.

## Application Startup

The following initialization steps run during the FastAPI lifespan event. All steps are idempotent and safe to run repeatedly.

1. **Directory creation** — Creates `CLEAN_ROOM_DIR`, `repos/`, and `specs-monorepo/` directories if they do not exist.
2. **Database initialization** — Runs the schema DDL to create tables. Existing tables are not modified (`CREATE TABLE IF NOT EXISTS`).
3. **Default prompt seeding** — If the `prompts` table is empty, inserts a set of default prompts. Existing prompts are not affected.
4. **Specs monorepo initialization** — Runs `git init` on the `specs-monorepo/` directory if it is not already a Git repository.

## Dependencies

### Core

| Package            | Version    | Purpose                              |
|--------------------|------------|--------------------------------------|
| `fastapi`          | >= 0.115   | Web framework                        |
| `uvicorn[standard]`| >= 0.34    | ASGI server                          |
| `jinja2`           | >= 3.1     | HTML template rendering              |
| `aiosqlite`        | >= 0.20    | Async SQLite database access         |
| `python-dotenv`    | >= 1.0     | Environment variable loading         |
| `claude-agent-sdk` | >= 0.1     | Claude Agent SDK for AI agent runs   |
| `sse-starlette`    | >= 2.0     | Server-Sent Events support           |

### Development

| Package           | Version    | Purpose                              |
|-------------------|------------|--------------------------------------|
| `pytest`          | >= 8.0     | Test runner                          |
| `pytest-asyncio`  | >= 0.24    | Async test support                   |
| `hypothesis`      | >= 6.100   | Property-based testing               |
| `httpx`           | >= 0.27    | HTTP client for test requests        |
| `ruff`            | >= 0.4     | Linter and formatter                 |
| `mypy`            | >= 1.10    | Static type checking                 |
